"""
API for accessing the multithreading extensions of PyPy
"""
import thread

try:
    from __pypy__ import thread as _thread
    from __pypy__.thread import atomic, getsegmentlimit, hint_commit_soon
except ImportError:
    # Not a STM-enabled PyPy.  We can still provide a version of 'atomic'
    # that is good enough for our purposes.  With this limited version,
    # an atomic block in thread X will not prevent running thread Y, if
    # thread Y is not within an atomic block at all.
    atomic = thread.allocate_lock()

    def getsegmentlimit():
        return 1

    def print_abort_info(mintime=0.0):
        pass

    def hint_commit_soon():
        pass

else:
    import re, sys, linecache

    _timing_reasons = [
        "'outside transaction'",
        "'run current'",
        "'run committed'",
        "'run aborted write write'",
        "'run aborted write read'",
        "'run aborted inevitable'",
        "'run aborted other'",
        "'wait free segment'",
        "'wait write read'",
        "'wait inevitable'",
        "'wait other'",
        "'sync commit soon'",
        "'bookkeeping'",
        "'minor gc'",
        "'major gc'",
        "'sync pause'",
        ]
    _r_line = re.compile(r'File "(.*?)[co]?", line (\d+), in ')
    _fullfilenames = {}

    def print_abort_info(mintime=0.0):
        info = _thread.longest_abort_info(mintime)
        if info is None:
            return

        output = []
        with atomic:
            output.append("Conflict ")
            a, b, c, d = info
            try:
                reason = _timing_reasons[a]
            except IndexError:
                reason = "'%s'" % (a,)
            output.append(reason)
            def show(line):
                output.append("  %s\n" % line)
                match = _r_line.match(line)
                if match and match.group(1) != '?':
                    filename = match.group(1)
                    lineno = int(match.group(2))
                    if filename.startswith('<') and not filename.endswith('>'):
                        if filename not in _fullfilenames:
                            partial = filename[1:]
                            found = set()
                            for module in sys.modules.values():
                                try:
                                    modfile = object.__getattribute__(module, '__file__')
                                except Exception:
                                    modfile = None
                                if type(modfile) is str and modfile.endswith(partial):
                                    found.add(modfile)
                            if len(found) == 1:
                                _fullfilenames[filename], = found
                            else:
                                _fullfilenames[filename] = None
                        filename = _fullfilenames[filename]
                    line = linecache.getline(filename, lineno)
                    if line:
                        output.append("    %s\n" % line.strip())
            if d:
                output.append(" between two threads:\n")
                show(c)
                show(d)
            else:
                output.append(" in this thread:\n")
                show(c)
            output.append('Lost %.6f seconds.\n\n' % (b,))
            _thread.reset_longest_abort_info()
        print >> sys.stderr, "".join(output),
