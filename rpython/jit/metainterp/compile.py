import weakref
from rpython.rtyper.lltypesystem import lltype, llmemory
from rpython.rtyper.annlowlevel import cast_instance_to_gcref
from rpython.rlib.objectmodel import we_are_translated
from rpython.rlib.debug import debug_start, debug_stop, debug_print
from rpython.rlib.rarithmetic import r_uint, intmask
from rpython.rlib import rstack
from rpython.rlib.jit import JitDebugInfo, Counters, dont_look_inside
from rpython.conftest import option

from rpython.jit.metainterp.resoperation import ResOperation, rop,\
     get_deep_immutable_oplist, OpHelpers, InputArgInt, InputArgRef,\
     InputArgFloat
from rpython.jit.metainterp.history import (TreeLoop, Const, JitCellToken,
    TargetToken, AbstractFailDescr, ConstInt)
from rpython.jit.metainterp import history, jitexc
from rpython.jit.metainterp.optimize import InvalidLoop
from rpython.jit.metainterp.resume import NUMBERING, PENDINGFIELDSP, ResumeDataDirectReader
from rpython.jit.codewriter import heaptracker, longlong


def giveup():
    from rpython.jit.metainterp.pyjitpl import SwitchToBlackhole
    raise SwitchToBlackhole(Counters.ABORT_BRIDGE)

class CompileData(object):
    def forget_optimization_info(self):
        for arg in self.start_label.getarglist():
            arg.set_forwarded(None)
        for op in self.operations:
            op.set_forwarded(None)

class LoopCompileData(CompileData):
    """ An object that accumulates all of the necessary info for
    the optimization phase, but does not actually have any other state

    This is the case of label() ops label()
    """
    def __init__(self, start_label, end_label, operations,
                 call_pure_results=None, enable_opts=None):
        self.start_label = start_label
        self.end_label = end_label
        self.enable_opts = enable_opts
        assert start_label.getopnum() == rop.LABEL
        assert end_label.getopnum() == rop.LABEL
        self.operations = operations
        self.call_pure_results = call_pure_results

    def optimize(self, metainterp_sd, jitdriver_sd, optimizations, unroll):
        from rpython.jit.metainterp.optimizeopt.unroll import (UnrollOptimizer,
                                                               Optimizer)

        if unroll:
            opt = UnrollOptimizer(metainterp_sd, jitdriver_sd, optimizations)
            return opt.optimize_preamble(self.start_label, self.end_label,
                                         self.operations,
                                         self.call_pure_results)
        else:
            opt = Optimizer(metainterp_sd, jitdriver_sd, optimizations)
            return opt.propagate_all_forward(self.start_label.getarglist(),
               self.operations, self.call_pure_results)

class SimpleCompileData(CompileData):
    """ This represents label() ops jump with no extra info associated with
    the label
    """
    def __init__(self, start_label, operations, call_pure_results=None,
                 enable_opts=None):
        self.start_label = start_label
        self.operations = operations
        self.call_pure_results = call_pure_results
        self.enable_opts = enable_opts

    def optimize(self, metainterp_sd, jitdriver_sd, optimizations, unroll):
        from rpython.jit.metainterp.optimizeopt.optimizer import Optimizer

        #assert not unroll
        opt = Optimizer(metainterp_sd, jitdriver_sd, optimizations)
        return opt.propagate_all_forward(self.start_label.getarglist(),
            self.operations, self.call_pure_results)

class BridgeCompileData(CompileData):
    """ This represents ops() with a jump at the end that goes to some
    loop, we need to deal with virtual state and inlining of short preamble
    """
    def __init__(self, start_label, operations, call_pure_results=None,
                 enable_opts=None, inline_short_preamble=False):
        self.start_label = start_label
        self.operations = operations
        self.call_pure_results = call_pure_results
        self.enable_opts = enable_opts
        self.inline_short_preamble = inline_short_preamble

    def optimize(self, metainterp_sd, jitdriver_sd, optimizations, unroll):
        from rpython.jit.metainterp.optimizeopt.unroll import UnrollOptimizer

        opt = UnrollOptimizer(metainterp_sd, jitdriver_sd, optimizations)
        return opt.optimize_bridge(self.start_label, self.operations,
                                   self.call_pure_results,
                                   self.inline_short_preamble)

class UnrolledLoopData(CompileData):
    """ This represents label() ops jump with extra info that's from the
    run of LoopCompileData. Jump goes to the same label
    """
    def __init__(self, start_label, end_jump, operations, state,
                 call_pure_results=None, enable_opts=None):
        self.start_label = start_label
        self.end_jump = end_jump
        self.operations = operations
        self.enable_opts = enable_opts
        self.state = state
        self.call_pure_results = call_pure_results

    def optimize(self, metainterp_sd, jitdriver_sd, optimizations, unroll):
        from rpython.jit.metainterp.optimizeopt.unroll import UnrollOptimizer

        assert unroll # we should not be here if it's disabled
        opt = UnrollOptimizer(metainterp_sd, jitdriver_sd, optimizations)
        return opt.optimize_peeled_loop(self.start_label, self.end_jump,
            self.operations, self.state, self.call_pure_results)

def show_procedures(metainterp_sd, procedure=None, error=None):
    # debugging
    if option and (option.view or option.viewloops):
        if error:
            errmsg = error.__class__.__name__
            if str(error):
                errmsg += ': ' + str(error)
        else:
            errmsg = None
        if procedure is None:
            extraprocedures = []
        else:
            extraprocedures = [procedure]
        metainterp_sd.stats.view(errmsg=errmsg,
                                 extraprocedures=extraprocedures,
                                 metainterp_sd=metainterp_sd)

def create_empty_loop(metainterp, name_prefix=''):
    name = metainterp.staticdata.stats.name_for_new_loop()
    loop = TreeLoop(name_prefix + name)
    return loop


def make_jitcell_token(jitdriver_sd):
    jitcell_token = JitCellToken()
    jitcell_token.outermost_jitdriver_sd = jitdriver_sd
    return jitcell_token

def record_loop_or_bridge(metainterp_sd, loop):
    """Do post-backend recordings and cleanups on 'loop'.
    """
    # get the original jitcell token corresponding to jitcell form which
    # this trace starts
    original_jitcell_token = loop.original_jitcell_token
    assert original_jitcell_token is not None
    if metainterp_sd.warmrunnerdesc is not None:    # for tests
        assert original_jitcell_token.generation > 0     # has been registered with memmgr
    wref = weakref.ref(original_jitcell_token)
    clt = original_jitcell_token.compiled_loop_token
    clt.loop_token_wref = wref
    for op in loop.operations:
        descr = op.getdescr()
        # not sure what descr.index is about
        if isinstance(descr, ResumeDescr):
            descr.rd_loop_token = clt   # stick it there
            #n = descr.index
            #if n >= 0:       # we also record the resumedescr number
            #    original_jitcell_token.compiled_loop_token.record_faildescr_index(n)
        #    pass
        if isinstance(descr, JitCellToken):
            # for a CALL_ASSEMBLER: record it as a potential jump.
            if descr is not original_jitcell_token:
                original_jitcell_token.record_jump_to(descr)
            op.cleardescr()    # clear reference, mostly for tests
        elif isinstance(descr, TargetToken):
            # for a JUMP: record it as a potential jump.
            # (the following test is not enough to prevent more complicated
            # cases of cycles, but at least it helps in simple tests of
            # test_memgr.py)
            if descr.original_jitcell_token is not original_jitcell_token:
                assert descr.original_jitcell_token is not None
                original_jitcell_token.record_jump_to(descr.original_jitcell_token)
            if not we_are_translated():
                op._descr_wref = weakref.ref(op._descr)
            op.cleardescr()    # clear reference to prevent the history.Stats
                               # from keeping the loop alive during tests
    # record this looptoken on the QuasiImmut used in the code
    if loop.quasi_immutable_deps is not None:
        for qmut in loop.quasi_immutable_deps:
            qmut.register_loop_token(wref)
        # XXX maybe we should clear the dictionary here
    # mostly for tests: make sure we don't keep a reference to the LoopToken
    loop.original_jitcell_token = None
    if not we_are_translated():
        loop._looptoken_number = original_jitcell_token.number

# ____________________________________________________________


def compile_simple_loop(metainterp, greenkey, start, inputargs, ops, jumpargs,
                        enable_opts):
    from rpython.jit.metainterp.optimizeopt import optimize_trace

    jitdriver_sd = metainterp.jitdriver_sd
    metainterp_sd = metainterp.staticdata
    jitcell_token = make_jitcell_token(jitdriver_sd)
    label = ResOperation(rop.LABEL, inputargs[:], descr=jitcell_token)
    jump_op = ResOperation(rop.JUMP, jumpargs[:], descr=jitcell_token)
    call_pure_results = metainterp.call_pure_results
    data = SimpleCompileData(label, ops + [jump_op],
                                 call_pure_results=call_pure_results,
                                 enable_opts=enable_opts)
    try:
        loop_info, ops = optimize_trace(metainterp_sd, jitdriver_sd,
                                        data)
    except InvalidLoop:
        return None
    loop = create_empty_loop(metainterp)
    loop.original_jitcell_token = jitcell_token
    loop.inputargs = loop_info.inputargs
    if loop_info.quasi_immutable_deps:
        loop.quasi_immutable_deps = loop_info.quasi_immutable_deps
    jump_op = ops[-1]
    target_token = TargetToken(jitcell_token)
    target_token.original_jitcell_token = jitcell_token
    label = ResOperation(rop.LABEL, loop_info.inputargs[:], descr=target_token)
    jump_op.setdescr(target_token)
    loop.operations = [label] + ops
    if not we_are_translated():
        loop.check_consistency()
    jitcell_token.target_tokens = [target_token]
    send_loop_to_backend(greenkey, jitdriver_sd, metainterp_sd, loop, "loop",
                         inputargs)
    record_loop_or_bridge(metainterp_sd, loop)
    return target_token

def compile_loop(metainterp, greenkey, start, inputargs, jumpargs,
                 full_preamble_needed=True, try_disabling_unroll=False):
    """Try to compile a new procedure by closing the current history back
    to the first operation.
    """
    from rpython.jit.metainterp.optimizeopt import optimize_trace

    metainterp_sd = metainterp.staticdata
    jitdriver_sd = metainterp.jitdriver_sd
    history = metainterp.history

    enable_opts = jitdriver_sd.warmstate.enable_opts
    if try_disabling_unroll:
        if 'unroll' not in enable_opts:
            return None
        enable_opts = enable_opts.copy()
        del enable_opts['unroll']

    ops = history.operations[start:]
    if 'unroll' not in enable_opts:
        return compile_simple_loop(metainterp, greenkey, start, inputargs, ops,
                                   jumpargs, enable_opts)
    jitcell_token = make_jitcell_token(jitdriver_sd)
    label = ResOperation(rop.LABEL, inputargs,
                         descr=TargetToken(jitcell_token))
    end_label = ResOperation(rop.LABEL, jumpargs, descr=jitcell_token)
    call_pure_results = metainterp.call_pure_results
    preamble_data = LoopCompileData(label, end_label, ops,
                                    call_pure_results=call_pure_results,
                                    enable_opts=enable_opts)
    try:
        start_state, preamble_ops = optimize_trace(metainterp_sd, jitdriver_sd,
                                                   preamble_data)
    except InvalidLoop:
        return None

    metainterp_sd = metainterp.staticdata
    jitdriver_sd = metainterp.jitdriver_sd
    end_label = ResOperation(rop.LABEL, inputargs,
                             descr=jitcell_token)
    jump_op = ResOperation(rop.JUMP, jumpargs, descr=jitcell_token)
    loop_data = UnrolledLoopData(end_label, jump_op, ops, start_state,
                                 call_pure_results=call_pure_results,
                                 enable_opts=enable_opts)
    try:
        loop_info, loop_ops = optimize_trace(metainterp_sd, jitdriver_sd,
                                             loop_data)
    except InvalidLoop:
        return None

    loop = create_empty_loop(metainterp)
    loop.original_jitcell_token = jitcell_token
    loop.inputargs = start_state.renamed_inputargs
    quasi_immutable_deps = {}
    if start_state.quasi_immutable_deps:
        quasi_immutable_deps.update(start_state.quasi_immutable_deps)
    if loop_info.quasi_immutable_deps:
        quasi_immutable_deps.update(loop_info.quasi_immutable_deps)
    if quasi_immutable_deps:
        loop.quasi_immutable_deps = quasi_immutable_deps
    start_descr = TargetToken(jitcell_token,
                              original_jitcell_token=jitcell_token)
    start_label = ResOperation(rop.LABEL, start_state.renamed_inputargs,
                               descr=start_descr)
    loop.operations = ([start_label] + preamble_ops + loop_info.extra_same_as +
                       [loop_info.label_op] + loop_ops)
    if not we_are_translated():
        loop.check_consistency()
    jitcell_token.target_tokens = [start_descr] + jitcell_token.target_tokens
    send_loop_to_backend(greenkey, jitdriver_sd, metainterp_sd, loop, "loop",
                         inputargs)
    record_loop_or_bridge(metainterp_sd, loop)
    return start_descr

def compile_retrace(metainterp, greenkey, start,
                    inputargs, jumpargs,
                    partial_trace, resumekey, start_state):
    """Try to compile a new procedure by closing the current history back
    to the first operation.
    """
    from rpython.jit.metainterp.optimizeopt import optimize_trace
    from rpython.jit.metainterp.optimizeopt.optimizer import BasicLoopInfo

    history = metainterp.history
    metainterp_sd = metainterp.staticdata
    jitdriver_sd = metainterp.jitdriver_sd

    loop_jitcell_token = metainterp.get_procedure_token(greenkey)
    assert loop_jitcell_token

    end_label = ResOperation(rop.LABEL, inputargs,
                             descr=loop_jitcell_token)
    jump_op = ResOperation(rop.JUMP, jumpargs, descr=loop_jitcell_token)
    enable_opts = jitdriver_sd.warmstate.enable_opts
    ops = history.operations[start:]
    call_pure_results = metainterp.call_pure_results
    loop_data = UnrolledLoopData(end_label, jump_op, ops, start_state,
                                 call_pure_results=call_pure_results,
                                 enable_opts=enable_opts)
    try:
        loop_info, loop_ops = optimize_trace(metainterp_sd, jitdriver_sd,
                                             loop_data)
    except InvalidLoop:
        # Fall back on jumping to preamble
        raise Exception("think about it")
        return None

    loop = partial_trace
    loop.original_jitcell_token = loop_jitcell_token
    loop.operations = (loop.operations + loop_info.extra_same_as +
                       [loop_info.label_op]
                       + loop_ops)

    quasi_immutable_deps = {}
    if loop_info.quasi_immutable_deps:
        quasi_immutable_deps.update(loop_info.quasi_immutable_deps)
    if start_state.quasi_immutable_deps:
        quasi_immutable_deps.update(start_state.quasi_immutable_deps)
    if quasi_immutable_deps:
        loop.quasi_immutable_deps = quasi_immutable_deps

    target_token = loop.operations[-1].getdescr()
    resumekey.compile_and_attach(metainterp, loop, inputargs)

    record_loop_or_bridge(metainterp_sd, loop)
    return target_token

def get_box_replacement(op, allow_none=False):
    if allow_none and op is None:
        return None # for failargs
    while op.get_forwarded():
        op = op.get_forwarded()
    return op

def emit_op(lst, op):
    op = get_box_replacement(op)
    orig_op = op
    # XXX specialize on number of args
    replaced = False
    for i in range(op.numargs()):
        orig_arg = op.getarg(i)
        arg = get_box_replacement(orig_arg)
        if orig_arg is not arg:
            if not replaced:
                op = op.copy_and_change(op.getopnum())
                orig_op.set_forwarded(op)
                replaced = True
            op.setarg(i, arg)
    if op.is_guard():
        if not replaced:
            op = op.copy_and_change(op.getopnum())
            orig_op.set_forwarded(op)
        op.setfailargs([get_box_replacement(a, True)
                        for a in op.getfailargs()])
    lst.append(op)

def patch_new_loop_to_load_virtualizable_fields(loop, jitdriver_sd, vable):
    # XXX merge with rewriting
    vinfo = jitdriver_sd.virtualizable_info
    extra_ops = []
    inputargs = loop.inputargs
    vable_box = inputargs[jitdriver_sd.index_of_virtualizable]
    i = jitdriver_sd.num_red_args
    loop.inputargs = inputargs[:i]
    for descr in vinfo.static_field_descrs:
        assert i < len(inputargs)
        box = inputargs[i]
        opnum = OpHelpers.getfield_for_descr(descr)
        emit_op(extra_ops,
                ResOperation(opnum, [vable_box], descr))
        box.set_forwarded(extra_ops[-1])
        i += 1
    arrayindex = 0
    for descr in vinfo.array_field_descrs:
        arraylen = vinfo.get_array_length(vable, arrayindex)
        arrayop = ResOperation(rop.GETFIELD_GC_R, [vable_box], descr)
        emit_op(extra_ops, arrayop)
        arraydescr = vinfo.array_descrs[arrayindex]
        assert i + arraylen <= len(inputargs)
        for index in range(arraylen):
            opnum = OpHelpers.getarrayitem_for_descr(arraydescr)
            box = inputargs[i]
            emit_op(extra_ops,
                ResOperation(opnum,
                             [arrayop, ConstInt(index)],
                             descr=arraydescr))
            i += 1
            box.set_forwarded(extra_ops[-1])
        arrayindex += 1
    assert i == len(inputargs)
    for op in loop.operations:
        emit_op(extra_ops, op)
    loop.operations = extra_ops

def propagate_original_jitcell_token(trace):
    for op in trace.operations:
        if op.getopnum() == rop.LABEL:
            token = op.getdescr()
            assert isinstance(token, TargetToken)
            token.original_jitcell_token = trace.original_jitcell_token


def do_compile_loop(jd_id, unique_id, metainterp_sd, inputargs, operations,
                    looptoken, log=True, name=''):
    metainterp_sd.logger_ops.log_loop(inputargs, operations, -2,
                                      'compiling', name=name)
    return metainterp_sd.cpu.compile_loop(inputargs,
                                          operations, looptoken,
                                          jd_id=jd_id, unique_id=unique_id,
                                          log=log, name=name,
                                          logger=metainterp_sd.logger_ops)

def do_compile_bridge(metainterp_sd, faildescr, inputargs, operations,
                      original_loop_token, log=True):
    metainterp_sd.logger_ops.log_bridge(inputargs, operations, "compiling")
    assert isinstance(faildescr, AbstractFailDescr)
    return metainterp_sd.cpu.compile_bridge(faildescr, inputargs, operations,
                                            original_loop_token, log=log,
                                            logger=metainterp_sd.logger_ops)

def forget_optimization_info(lst, reset_values=False):
    for item in lst:
        item.set_forwarded(None)
        # XXX we should really do it, but we need to remember the values
        #     somehoe for ContinueRunningNormally
        if reset_values:
            item.reset_value()

def send_loop_to_backend(greenkey, jitdriver_sd, metainterp_sd, loop, type,
                         orig_inpargs):
    forget_optimization_info(loop.operations)
    forget_optimization_info(loop.inputargs)
    vinfo = jitdriver_sd.virtualizable_info
    if vinfo is not None:
        vable = orig_inpargs[jitdriver_sd.index_of_virtualizable].getref_base()
        patch_new_loop_to_load_virtualizable_fields(loop, jitdriver_sd, vable)

    original_jitcell_token = loop.original_jitcell_token
    globaldata = metainterp_sd.globaldata
    original_jitcell_token.number = n = globaldata.loopnumbering
    globaldata.loopnumbering += 1

    if not we_are_translated():
        show_procedures(metainterp_sd, loop)
        loop.check_consistency()

    if metainterp_sd.warmrunnerdesc is not None:
        hooks = metainterp_sd.warmrunnerdesc.hooks
        debug_info = JitDebugInfo(jitdriver_sd, metainterp_sd.logger_ops,
                                  original_jitcell_token, loop.operations,
                                  type, greenkey)
        hooks.before_compile(debug_info)
    else:
        debug_info = None
        hooks = None
    operations = get_deep_immutable_oplist(loop.operations)
    metainterp_sd.profiler.start_backend()
    debug_start("jit-backend")
    try:
        loopname = jitdriver_sd.warmstate.get_location_str(greenkey)
        unique_id = jitdriver_sd.warmstate.get_unique_id(greenkey)
        asminfo = do_compile_loop(jitdriver_sd.index, unique_id, metainterp_sd,
                                  loop.inputargs,
                                  operations, original_jitcell_token,
                                  name=loopname)
    finally:
        debug_stop("jit-backend")
    metainterp_sd.profiler.end_backend()
    if hooks is not None:
        debug_info.asminfo = asminfo
        hooks.after_compile(debug_info)
    metainterp_sd.stats.add_new_loop(loop)
    if not we_are_translated():
        metainterp_sd.stats.compiled()
    metainterp_sd.log("compiled new " + type)
    #
    if asminfo is not None:
        ops_offset = asminfo.ops_offset
    else:
        ops_offset = None
    metainterp_sd.logger_ops.log_loop(loop.inputargs, loop.operations, n,
                                      type, ops_offset,
                                      name=loopname)
    #
    if metainterp_sd.warmrunnerdesc is not None:    # for tests
        metainterp_sd.warmrunnerdesc.memory_manager.keep_loop_alive(original_jitcell_token)

def send_bridge_to_backend(jitdriver_sd, metainterp_sd, faildescr, inputargs,
                           operations, original_loop_token):
    forget_optimization_info(operations)
    forget_optimization_info(inputargs)
    if not we_are_translated():
        show_procedures(metainterp_sd)
        seen = dict.fromkeys(inputargs)
        TreeLoop.check_consistency_of_branch(operations, seen)
    if metainterp_sd.warmrunnerdesc is not None:
        hooks = metainterp_sd.warmrunnerdesc.hooks
        debug_info = JitDebugInfo(jitdriver_sd, metainterp_sd.logger_ops,
                                  original_loop_token, operations, 'bridge',
                                  fail_descr=faildescr)
        hooks.before_compile_bridge(debug_info)
    else:
        hooks = None
        debug_info = None
    operations = get_deep_immutable_oplist(operations)
    metainterp_sd.profiler.start_backend()
    debug_start("jit-backend")
    try:
        asminfo = do_compile_bridge(metainterp_sd, faildescr, inputargs,
                                    operations,
                                    original_loop_token)
    finally:
        debug_stop("jit-backend")
    metainterp_sd.profiler.end_backend()
    if hooks is not None:
        debug_info.asminfo = asminfo
        hooks.after_compile_bridge(debug_info)
    if not we_are_translated():
        metainterp_sd.stats.compiled()
    metainterp_sd.log("compiled new bridge")
    #
    if asminfo is not None:
        ops_offset = asminfo.ops_offset
    else:
        ops_offset = None
    metainterp_sd.logger_ops.log_bridge(inputargs, operations, None, faildescr,
                                        ops_offset)
    #
    #if metainterp_sd.warmrunnerdesc is not None:    # for tests
    #    metainterp_sd.warmrunnerdesc.memory_manager.keep_loop_alive(
    #        original_loop_token)

# ____________________________________________________________

class _DoneWithThisFrameDescr(AbstractFailDescr):
    final_descr = True

class DoneWithThisFrameDescrVoid(_DoneWithThisFrameDescr):
    def handle_fail(self, deadframe, metainterp_sd, jitdriver_sd):
        assert jitdriver_sd.result_type == history.VOID
        raise jitexc.DoneWithThisFrameVoid()

class DoneWithThisFrameDescrInt(_DoneWithThisFrameDescr):
    def handle_fail(self, deadframe, metainterp_sd, jitdriver_sd):
        assert jitdriver_sd.result_type == history.INT
        result = metainterp_sd.cpu.get_int_value(deadframe, 0)
        raise jitexc.DoneWithThisFrameInt(result)

class DoneWithThisFrameDescrRef(_DoneWithThisFrameDescr):
    def handle_fail(self, deadframe, metainterp_sd, jitdriver_sd):
        assert jitdriver_sd.result_type == history.REF
        cpu = metainterp_sd.cpu
        result = cpu.get_ref_value(deadframe, 0)
        raise jitexc.DoneWithThisFrameRef(cpu, result)

class DoneWithThisFrameDescrFloat(_DoneWithThisFrameDescr):
    def handle_fail(self, deadframe, metainterp_sd, jitdriver_sd):
        assert jitdriver_sd.result_type == history.FLOAT
        result = metainterp_sd.cpu.get_float_value(deadframe, 0)
        raise jitexc.DoneWithThisFrameFloat(result)

class ExitFrameWithExceptionDescrRef(_DoneWithThisFrameDescr):
    def handle_fail(self, deadframe, metainterp_sd, jitdriver_sd):
        cpu = metainterp_sd.cpu
        value = cpu.get_ref_value(deadframe, 0)
        raise jitexc.ExitFrameWithExceptionRef(cpu, value)


class TerminatingLoopToken(JitCellToken): # FIXME: kill?
    terminating = True

    def __init__(self, nargs, finishdescr):
        self.finishdescr = finishdescr

def make_done_loop_tokens():
    done_with_this_frame_descr_void = DoneWithThisFrameDescrVoid()
    done_with_this_frame_descr_int = DoneWithThisFrameDescrInt()
    done_with_this_frame_descr_ref = DoneWithThisFrameDescrRef()
    done_with_this_frame_descr_float = DoneWithThisFrameDescrFloat()
    exit_frame_with_exception_descr_ref = ExitFrameWithExceptionDescrRef()

    # pseudo loop tokens to make the life of optimize.py easier
    d = {'loop_tokens_done_with_this_frame_int': [
                TerminatingLoopToken(1, done_with_this_frame_descr_int)
                ],
            'loop_tokens_done_with_this_frame_ref': [
                TerminatingLoopToken(1, done_with_this_frame_descr_ref)
                ],
            'loop_tokens_done_with_this_frame_float': [
                TerminatingLoopToken(1, done_with_this_frame_descr_float)
                ],
            'loop_tokens_done_with_this_frame_void': [
                TerminatingLoopToken(0, done_with_this_frame_descr_void)
                ],
            'loop_tokens_exit_frame_with_exception_ref': [
                TerminatingLoopToken(1, exit_frame_with_exception_descr_ref)
                ],
    }
    d.update(locals())
    return d

class ResumeDescr(AbstractFailDescr):
    _attrs_ = ()

class ResumeGuardDescr(ResumeDescr):
    _attrs_ = ('rd_numb', 'rd_count', 'rd_consts', 'rd_virtuals',
               'rd_frame_info_list', 'rd_pendingfields', 'status')
    
    rd_numb = lltype.nullptr(NUMBERING)
    rd_count = 0
    rd_consts = None
    rd_virtuals = None
    rd_frame_info_list = None
    rd_pendingfields = lltype.nullptr(PENDINGFIELDSP.TO)

    status = r_uint(0)

    def copy_all_attributes_from(self, other):
        assert isinstance(other, ResumeGuardDescr)
        self.rd_count = other.rd_count
        self.rd_consts = other.rd_consts
        self.rd_frame_info_list = other.rd_frame_info_list
        self.rd_pendingfields = other.rd_pendingfields
        self.rd_virtuals = other.rd_virtuals
        self.rd_numb = other.rd_numb
        # we don't copy status

    ST_BUSY_FLAG    = 0x01     # if set, busy tracing from the guard
    ST_TYPE_MASK    = 0x06     # mask for the type (TY_xxx)
    ST_SHIFT        = 3        # in "status >> ST_SHIFT" is stored:
                               # - if TY_NONE, the jitcounter hash directly
                               # - otherwise, the guard_value failarg index
    ST_SHIFT_MASK   = -(1 << ST_SHIFT)
    TY_NONE         = 0x00
    TY_INT          = 0x02
    TY_REF          = 0x04
    TY_FLOAT        = 0x06

    def store_final_boxes(self, guard_op, boxes, metainterp_sd):
        guard_op.setfailargs(boxes)
        self.rd_count = len(boxes)
        #
        if metainterp_sd.warmrunnerdesc is not None:   # for tests
            jitcounter = metainterp_sd.warmrunnerdesc.jitcounter
            hash = jitcounter.fetch_next_hash()
            self.status = hash & self.ST_SHIFT_MASK

    def handle_fail(self, deadframe, metainterp_sd, jitdriver_sd):
        if self.must_compile(deadframe, metainterp_sd, jitdriver_sd):
            self.start_compiling()
            try:
                self._trace_and_compile_from_bridge(deadframe, metainterp_sd,
                                                    jitdriver_sd)
            finally:
                self.done_compiling()
        else:
            from rpython.jit.metainterp.blackhole import resume_in_blackhole
            resume_in_blackhole(metainterp_sd, jitdriver_sd, self, deadframe)
        assert 0, "unreachable"

    def _trace_and_compile_from_bridge(self, deadframe, metainterp_sd,
                                       jitdriver_sd):
        # 'jitdriver_sd' corresponds to the outermost one, i.e. the one
        # of the jit_merge_point where we started the loop, even if the
        # loop itself may contain temporarily recursion into other
        # jitdrivers.
        from rpython.jit.metainterp.pyjitpl import MetaInterp
        metainterp = MetaInterp(metainterp_sd, jitdriver_sd)
        metainterp.handle_guard_failure(self, deadframe)
    _trace_and_compile_from_bridge._dont_inline_ = True

    def must_compile(self, deadframe, metainterp_sd, jitdriver_sd):
        jitcounter = metainterp_sd.warmrunnerdesc.jitcounter
        #
        if self.status & (self.ST_BUSY_FLAG | self.ST_TYPE_MASK) == 0:
            # common case: this is not a guard_value, and we are not
            # already busy tracing.  The rest of self.status stores a
            # valid per-guard index in the jitcounter.
            hash = self.status
            assert hash == (self.status & self.ST_SHIFT_MASK)
        #
        # do we have the BUSY flag?  If so, we're tracing right now, e.g. in an
        # outer invocation of the same function, so don't trace again for now.
        elif self.status & self.ST_BUSY_FLAG:
            return False
        #
        else:    # we have a GUARD_VALUE that fails.
            from rpython.rlib.objectmodel import current_object_addr_as_int

            index = intmask(self.status >> self.ST_SHIFT)
            typetag = intmask(self.status & self.ST_TYPE_MASK)

            # fetch the actual value of the guard_value, possibly turning
            # it to an integer
            if typetag == self.TY_INT:
                intval = metainterp_sd.cpu.get_int_value(deadframe, index)
            elif typetag == self.TY_REF:
                refval = metainterp_sd.cpu.get_ref_value(deadframe, index)
                intval = lltype.cast_ptr_to_int(refval)
            elif typetag == self.TY_FLOAT:
                floatval = metainterp_sd.cpu.get_float_value(deadframe, index)
                intval = longlong.gethash_fast(floatval)
            else:
                assert 0, typetag

            if not we_are_translated():
                if isinstance(intval, llmemory.AddressAsInt):
                    intval = llmemory.cast_adr_to_int(
                        llmemory.cast_int_to_adr(intval), "forced")

            hash = r_uint(current_object_addr_as_int(self) * 777767777 +
                          intval * 1442968193)
        #
        increment = jitdriver_sd.warmstate.increment_trace_eagerness
        return jitcounter.tick(hash, increment)

    def get_index_of_guard_value(self):
        if (self.status & self.ST_TYPE_MASK) == 0:
            return -1
        return intmask(self.status >> self.ST_SHIFT)

    def start_compiling(self):
        # start tracing and compiling from this guard.
        self.status |= self.ST_BUSY_FLAG

    def done_compiling(self):
        # done tracing and compiling from this guard.  Note that if the
        # bridge has not been successfully compiled, the jitcounter for
        # it was reset to 0 already by jitcounter.tick() and not
        # incremented at all as long as ST_BUSY_FLAG was set.
        self.status &= ~self.ST_BUSY_FLAG

    def compile_and_attach(self, metainterp, new_loop, orig_inputargs):
        # We managed to create a bridge.  Attach the new operations
        # to the corresponding guard_op and compile from there
        assert metainterp.resumekey_original_loop_token is not None
        new_loop.original_jitcell_token = metainterp.resumekey_original_loop_token
        inputargs = new_loop.inputargs
        if not we_are_translated():
            self._debug_suboperations = new_loop.operations
        propagate_original_jitcell_token(new_loop)
        send_bridge_to_backend(metainterp.jitdriver_sd, metainterp.staticdata,
                               self, inputargs, new_loop.operations,
                               new_loop.original_jitcell_token)

    def make_a_counter_per_value(self, guard_value_op):
        assert guard_value_op.getopnum() == rop.GUARD_VALUE
        box = guard_value_op.getarg(0)
        try:
            i = guard_value_op.getfailargs().index(box)
        except ValueError:
            return     # xxx probably very rare
        else:
            if box.type == history.INT:
                ty = self.TY_INT
            elif box.type == history.REF:
                ty = self.TY_REF
            elif box.type == history.FLOAT:
                ty = self.TY_FLOAT
            else:
                assert 0, box.type
            self.status = ty | (r_uint(i) << self.ST_SHIFT)

class ResumeGuardNonnullDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_NONNULL

class ResumeGuardIsnullDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_ISNULL

class ResumeGuardClassDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_CLASS

class ResumeGuardTrueDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_TRUE

class ResumeGuardFalseDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_FALSE

class ResumeGuardNonnullClassDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_NONNULL_CLASS

class ResumeGuardExceptionDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_EXCEPTION

class ResumeGuardNoExceptionDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_NO_EXCEPTION

class ResumeGuardOverflowDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_OVERFLOW

class ResumeGuardNoOverflowDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_NO_OVERFLOW

class ResumeGuardValueDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_VALUE

class ResumeGuardNotInvalidated(ResumeGuardDescr):
    guard_opnum = rop.GUARD_NOT_INVALIDATED

class ResumeAtPositionDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_FUTURE_CONDITION

class AllVirtuals:
    llopaque = True
    cache = None

    def __init__(self, cache):
        self.cache = cache

    def hide(self, cpu):
        ptr = cpu.ts.cast_instance_to_base_ref(self)
        return cpu.ts.cast_to_ref(ptr)

    @staticmethod
    def show(cpu, gcref):
        from rpython.rtyper.annlowlevel import cast_base_ptr_to_instance
        ptr = cpu.ts.cast_to_baseclass(gcref)
        return cast_base_ptr_to_instance(AllVirtuals, ptr)


class ResumeGuardForcedDescr(ResumeGuardDescr):
    guard_opnum = rop.GUARD_NOT_FORCED

    def _init(self, metainterp_sd, jitdriver_sd):
        # to please the annotator
        self.metainterp_sd = metainterp_sd
        self.jitdriver_sd = jitdriver_sd

    def handle_fail(self, deadframe, metainterp_sd, jitdriver_sd):
        # Failures of a GUARD_NOT_FORCED are never compiled, but
        # always just blackholed.  First fish for the data saved when
        # the virtualrefs and virtualizable have been forced by
        # handle_async_forcing() just a moment ago.
        from rpython.jit.metainterp.blackhole import resume_in_blackhole
        hidden_all_virtuals = metainterp_sd.cpu.get_savedata_ref(deadframe)
        obj = AllVirtuals.show(metainterp_sd.cpu, hidden_all_virtuals)
        all_virtuals = obj.cache
        if all_virtuals is None:
            all_virtuals = ResumeDataDirectReader.VirtualCache([], [])
        assert jitdriver_sd is self.jitdriver_sd
        resume_in_blackhole(metainterp_sd, jitdriver_sd, self, deadframe,
                            all_virtuals)
        assert 0, "unreachable"

    @staticmethod
    @dont_look_inside
    def force_now(cpu, token):
        # Called during a residual call from the assembler, if the code
        # actually needs to force one of the virtualrefs or the virtualizable.
        # Implemented by forcing *all* virtualrefs and the virtualizable.

        # don't interrupt me! If the stack runs out in force_from_resumedata()
        # then we have seen cpu.force() but not self.save_data(), leaving in
        # an inconsistent state
        rstack._stack_criticalcode_start()
        try:
            deadframe = cpu.force(token)
            # this should set descr to ResumeGuardForceDescr, if it
            # was not that already
            faildescr = cpu.get_latest_descr(deadframe)
            assert isinstance(faildescr, ResumeGuardForcedDescr)
            faildescr.handle_async_forcing(deadframe)
        finally:
            rstack._stack_criticalcode_stop()

    def handle_async_forcing(self, deadframe):
        from rpython.jit.metainterp.resume import force_from_resumedata
        metainterp_sd = self.metainterp_sd
        vinfo = self.jitdriver_sd.virtualizable_info
        ginfo = self.jitdriver_sd.greenfield_info
        # there is some chance that this is already forced. In this case
        # the virtualizable would have a token = NULL
        all_virtuals = force_from_resumedata(metainterp_sd, self, deadframe,
                                             vinfo, ginfo)
        # The virtualizable data was stored on the real virtualizable above.
        # Handle all_virtuals: keep them for later blackholing from the
        # future failure of the GUARD_NOT_FORCED
        obj = AllVirtuals(all_virtuals)
        hidden_all_virtuals = obj.hide(metainterp_sd.cpu)
        metainterp_sd.cpu.set_savedata_ref(deadframe, hidden_all_virtuals)

def invent_fail_descr_for_op(opnum, optimizer):
    if opnum == rop.GUARD_NOT_FORCED or opnum == rop.GUARD_NOT_FORCED_2:
        resumedescr = ResumeGuardForcedDescr()
        resumedescr._init(optimizer.metainterp_sd, optimizer.jitdriver_sd)
    elif opnum == rop.GUARD_NOT_INVALIDATED:
        resumedescr = ResumeGuardNotInvalidated()
    elif opnum == rop.GUARD_FUTURE_CONDITION:
        resumedescr = ResumeAtPositionDescr()
    elif opnum == rop.GUARD_VALUE:
        resumedescr = ResumeGuardValueDescr()
    elif opnum == rop.GUARD_NONNULL:
        resumedescr = ResumeGuardNonnullDescr()
    elif opnum == rop.GUARD_ISNULL:
        resumedescr = ResumeGuardIsnullDescr()
    elif opnum == rop.GUARD_NONNULL_CLASS:
        resumedescr = ResumeGuardNonnullClassDescr()
    elif opnum == rop.GUARD_CLASS:
        resumedescr = ResumeGuardClassDescr()
    elif opnum == rop.GUARD_TRUE:
        resumedescr = ResumeGuardTrueDescr()
    elif opnum == rop.GUARD_FALSE:
        resumedescr = ResumeGuardFalseDescr()
    elif opnum == rop.GUARD_EXCEPTION:
        resumedescr = ResumeGuardExceptionDescr()
    elif opnum == rop.GUARD_NO_EXCEPTION:
        resumedescr = ResumeGuardNoExceptionDescr()
    elif opnum == rop.GUARD_OVERFLOW:
        resumedescr = ResumeGuardOverflowDescr()
    elif opnum == rop.GUARD_NO_OVERFLOW:
        resumedescr = ResumeGuardNoOverflowDescr()
    else:
        assert False
    return resumedescr

class ResumeFromInterpDescr(ResumeDescr):
    def __init__(self, original_greenkey):
        self.original_greenkey = original_greenkey

    def compile_and_attach(self, metainterp, new_loop, orig_inputargs):
        # We managed to create a bridge going from the interpreter
        # to previously-compiled code.  We keep 'new_loop', which is not
        # a loop at all but ends in a jump to the target loop.  It starts
        # with completely unoptimized arguments, as in the interpreter.
        metainterp_sd = metainterp.staticdata
        jitdriver_sd = metainterp.jitdriver_sd
        new_loop.original_jitcell_token = jitcell_token = make_jitcell_token(jitdriver_sd)
        propagate_original_jitcell_token(new_loop)
        send_loop_to_backend(self.original_greenkey, metainterp.jitdriver_sd,
                             metainterp_sd, new_loop, "entry bridge",
                             orig_inputargs)
        # send the new_loop to warmspot.py, to be called directly the next time
        jitdriver_sd.warmstate.attach_procedure_to_interp(
            self.original_greenkey, jitcell_token)
        metainterp_sd.stats.add_jitcell_token(jitcell_token)


def compile_trace(metainterp, resumekey):
    """Try to compile a new bridge leading from the beginning of the history
    to some existing place.
    """

    from rpython.jit.metainterp.optimizeopt import optimize_trace

    # The history contains new operations to attach as the code for the
    # failure of 'resumekey.guard_op'.
    #
    # Attempt to use optimize_bridge().  This may return None in case
    # it does not work -- i.e. none of the existing old_loop_tokens match.
    #new_trace = create_empty_loop(metainterp)
    #new_trace.inputargs = metainterp.history.inputargs[:]

    #new_trace.operations = metainterp.history.operations[:]
    metainterp_sd = metainterp.staticdata
    jitdriver_sd = metainterp.jitdriver_sd
    state = jitdriver_sd.warmstate
    if isinstance(resumekey, ResumeAtPositionDescr):
        inline_short_preamble = False
    else:
        inline_short_preamble = True
    inputargs = metainterp.history.inputargs[:]
    operations = metainterp.history.operations
    label = ResOperation(rop.LABEL, inputargs)
    jitdriver_sd = metainterp.jitdriver_sd
    enable_opts = jitdriver_sd.warmstate.enable_opts

    call_pure_results = metainterp.call_pure_results

    if operations[-1].getopnum() == rop.JUMP:
        data = BridgeCompileData(label, operations[:],
                                 call_pure_results=call_pure_results,
                                 enable_opts=enable_opts,
                                 inline_short_preamble=inline_short_preamble)
    else:
        data = SimpleCompileData(label, operations[:],
                                 call_pure_results=call_pure_results,
                                 enable_opts=enable_opts)
    try:
        info, newops = optimize_trace(metainterp_sd, jitdriver_sd, data)
    except InvalidLoop:
        debug_print("compile_new_bridge: got an InvalidLoop")
        # XXX I am fairly convinced that optimize_bridge cannot actually raise
        # InvalidLoop
        debug_print('InvalidLoop in compile_new_bridge')
        return None

    new_trace = create_empty_loop(metainterp)
    new_trace.operations = newops
    if info.quasi_immutable_deps:
        new_trace.quasi_immutable_deps = info.quasi_immutable_deps
    if info.final():
        new_trace.inputargs = info.inputargs
        target_token = new_trace.operations[-1].getdescr()
        resumekey.compile_and_attach(metainterp, new_trace, inputargs)
        record_loop_or_bridge(metainterp_sd, new_trace)
        return target_token
    new_trace.inputargs = info.renamed_inputargs
    metainterp.retrace_needed(new_trace, info)
    return None

# ____________________________________________________________

memory_error = MemoryError()

class PropagateExceptionDescr(AbstractFailDescr):
    def handle_fail(self, deadframe, metainterp_sd, jitdriver_sd):
        cpu = metainterp_sd.cpu
        exception = cpu.grab_exc_value(deadframe)
        if not exception:
            exception = cast_instance_to_gcref(memory_error)
        assert exception, "PropagateExceptionDescr: no exception??"
        raise jitexc.ExitFrameWithExceptionRef(cpu, exception)

def compile_tmp_callback(cpu, jitdriver_sd, greenboxes, redargtypes,
                         memory_manager=None):
    """Make a LoopToken that corresponds to assembler code that just
    calls back the interpreter.  Used temporarily: a fully compiled
    version of the code may end up replacing it.
    """
    jitcell_token = make_jitcell_token(jitdriver_sd)
    nb_red_args = jitdriver_sd.num_red_args
    assert len(redargtypes) == nb_red_args
    inputargs = []
    for kind in redargtypes:
        if kind == history.INT:
            box = InputArgInt()
        elif kind == history.REF:
            box = InputArgRef()
        elif kind == history.FLOAT:
            box = InputArgFloat()
        else:
            raise AssertionError
        inputargs.append(box)
    k = jitdriver_sd.portal_runner_adr
    funcbox = history.ConstInt(heaptracker.adr2int(k))
    callargs = [funcbox] + greenboxes + inputargs
    #

    jd = jitdriver_sd
    opnum = OpHelpers.call_for_descr(jd.portal_calldescr)
    call_op = ResOperation(opnum, callargs, descr=jd.portal_calldescr)
    if call_op.type != 'v' is not None:
        finishargs = [call_op]
    else:
        finishargs = []
    #
    faildescr = jitdriver_sd.propagate_exc_descr
    operations = [
        call_op,
        ResOperation(rop.GUARD_NO_EXCEPTION, [], descr=faildescr),
        ResOperation(rop.FINISH, finishargs, descr=jd.portal_finishtoken)
    ]
    operations[1].setfailargs([])
    operations = get_deep_immutable_oplist(operations)
    cpu.compile_loop(inputargs, operations, jitcell_token, log=False)
    if memory_manager is not None:    # for tests
        memory_manager.keep_loop_alive(jitcell_token)
    return jitcell_token
