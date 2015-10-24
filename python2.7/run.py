#! /usr/bin/env python

from __future__ import print_function
import __builtin__ as builtin_mod
import code
from cStringIO import StringIO
from namedlist import namedtuple
import os
from os import path
import sys
import types
import zmq

ExceptionInfo = namedtuple('ExceptionInfo', [
    'exc',
    ('args', tuple()),
    ('raised_before_exec', False),
    ('traceback', None),
])

@staticmethod
def _create_excinfo(e, raised_before_exec, tb):
    assert isinstance(e, Exception)
    return ExceptionInfo(type(e).__name__, e.args, raised_before_exec, tb)
ExceptionInfo.create = _create_excinfo


class SockWriter(object):
    def __init__(self, sock, cell_id):
        self.cell_id_encoded = '{0}'.format(cell_id).encode('ascii')
        self.sock = sock
        self.buffer = StringIO()

    def write(self, s):
        if '\n' in s:  # flush on occurrence of a newline.
            s1, s2 = s.split('\n', maxsplit=1)
            s0 = self.buffer.getvalue()
            self.sock.send_multipart([self.cell_id_encoded, (s0 + s1 + '\n').encode('utf8')])
            self.buffer.seek(0, os.SEEK_SET)
            self.buffer.truncate(0)
            self.buffer.write(s2)
        else:
            self.buffer.write(s)
        if self.buffer.tell() > 1024:  # flush if the buffer is too large.
            s0 = self.buffer.getvalue()
            self.sock.send_multipart([self.cell_id_encoded, s0.encode('utf8')])
            self.buffer.seek(0, os.SEEK_SET)
            self.buffer.truncate(0)
        # TODO: timeout to flush?


class CodeRunner(object):
    '''
    A thin wrapper for REPL.

    It creates a dummy module that user codes run and keeps the references to user-created objects
    (e.g., variables and functions).
    '''

    def __init__(self):
        self.stdout_buffer = StringIO()
        self.stderr_buffer = StringIO()

        # Initialize user module and namespaces.
        user_module = types.ModuleType('__main__',
                                       doc='Automatically created module for the interactive shell.')
        user_module.__dict__.setdefault('__builtin__', builtin_mod)
        user_module.__dict__.setdefault('__builtins__', builtin_mod)
        self.user_module = user_module
        self.user_ns = user_module.__dict__

    def execute(self, cell_id, src):
        self.stdout_writer = self.stdout_buffer
        self.stderr_writer = self.stderr_buffer
        sys.stdout, orig_stdout = self.stdout_writer, sys.stdout
        sys.stderr, orig_stderr = self.stderr_writer, sys.stderr

        exec_result = None
        exceptions = []
        before_exec = True

        def my_excepthook(type_, value, tb):
            exceptions.append(ExceptionInfo.create(value, before_exec, tb))
        sys.excepthook = my_excepthook

        try:
            code_obj = code.compile_command(src, symbol='exec')
        except IndentationError as e:
            exceptions.append(ExceptionInfo.create(e, before_exec, None))
        except (OverflowError, SyntaxError, ValueError, TypeError, MemoryError) as e:
            exceptions.append(ExceptionInfo.create(e, before_exec, None))
        else:
            before_exec = False
            exec_result = None
            try:
                exec code_obj in self.user_ns
            except Exception as e:
                exceptions.append(ExceptionInfo.create(e, before_exec, None))

        sys.excepthook = sys.__excepthook__

        output = (self.stdout_writer.buffer.getvalue(), self.stderr_writer.buffer.getvalue())
        self.stdout_writer.seek(0, os.SEEK_SET)
        self.stdout_writer.truncate(0)
        self.stderr_writer.seek(0, os.SEEK_SET)
        self.stderr_writer.truncate(0)

        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        return exec_result, exceptions, output


if __name__ == '__main__':
    # Use the "confined" working directory
    os.chdir('/home/work')
    # Replace stdin with a "null" file
    # (trying to read stdin will raise EOFError immediately afterwards.)
    sys.stdin = open(os.devnull, 'rb')

    # Initialize context object.
    runner = CodeRunner()

    # Initialize minimal ZMQ server socket.
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind('tcp://*:2001')

    # Apply the security sandbox.
    # TODO: implement and call initialize_ptrace_sandbox()

    try:
        while True:
            data = sock.recv_multipart()
            result, exceptions, output = runner.execute(data[0], data[1])
            if not (isinstance(exec_result, basestring) or exec_result is None):
                exec_result = unicode(exec_result)
            response = {
                'eval_result': exec_result,
                'stdout': output[0],
                'stderr': output[1],
                'exceptions': exceptions,
            }
            sock.send_json(response)
    except SystemExit:
        pass
    finally:
        sock.close()
        ctx.close()
