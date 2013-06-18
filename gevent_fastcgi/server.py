# Copyright (c) 2011-2013, Alexander Kulakov
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

from __future__ import with_statement, absolute_import

from gevent.monkey import patch_os
patch_os()

import os
import errno
import logging
from signal import SIGHUP, SIGINT, SIGQUIT, SIGKILL

from zope.interface import implements

from gevent import sleep, spawn, socket, signal, getcurrent
from gevent.server import StreamServer
from gevent.event import Event
from gevent.coros import Semaphore

from .interfaces import IRequest
from .const import (
    FCGI_ABORT_REQUEST,
    FCGI_AUTHORIZER,
    FCGI_BEGIN_REQUEST,
    FCGI_DATA,
    FCGI_END_REQUEST,
    FCGI_FILTER,
    FCGI_GET_VALUES,
    FCGI_GET_VALUES_RESULT,
    FCGI_KEEP_CONN,
    FCGI_NULL_REQUEST_ID,
    FCGI_PARAMS,
    FCGI_REQUEST_COMPLETE,
    FCGI_RESPONDER,
    FCGI_STDIN,
    FCGI_STDOUT,
    FCGI_STDERR,
    FCGI_DATA,
    FCGI_UNKNOWN_ROLE,
    FCGI_UNKNOWN_TYPE,
    EXISTING_REQUEST_RECORD_TYPES,
)
from .base import (
    Connection,
    Record,
    InputStream,
    OutputStream,
)
from .utils import (
    pack_pairs,
    unpack_pairs,
    pack_begin_request,
    unpack_begin_request,
    pack_end_request,
    unpack_end_request,
    pack_unknown_type,
)


__all__ = ('Request', 'ServerConnection', 'FastCGIServer')

logger = logging.getLogger(__name__)


class Request(object):

    implements(IRequest)

    def __init__(self, conn, request_id, role):
        self.conn = conn
        self.id = request_id
        self.role = role
        self.environ = {}
        self.stdin = InputStream()
        self.stdout = OutputStream(conn, request_id, FCGI_STDOUT)
        self.stderr = OutputStream(conn, request_id, FCGI_STDERR)
        self._greenlet = None
        self._environ = InputStream()


class ServerConnection(Connection):

    def __init__(self, *args, **kw):
        Connection.__init__(self, *args, **kw)
        self.lock = Semaphore()

    def write_record(self, record):
        # We must serialize access for possible multiple request greenlets
        self.lock.acquire()
        try:
            Connection.write_record(self, record)
        finally:
            self.lock.release()


class ConnectionHandler(object):

    def __init__(self, conn, role, capabilities, request_handler):
        conn._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        conn._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        self.conn = conn
        self.role = role
        self.capabilities = capabilities
        self.request_handler = request_handler
        self.requests = {}
        self.keep_open = None
        self.closing = False
        self._event = Event()

    def send_record(
            self, record_type, content='', request_id=FCGI_NULL_REQUEST_ID):
        self.conn.write_record(Record(record_type, content, request_id))

    def fcgi_begin_request(self, record):
        role, flags = unpack_begin_request(record.content)
        if role != self.role:
            self.send_record(FCGI_END_REQUEST, pack_end_request(
                0,  FCGI_UNKNOWN_ROLE), record.request_id)
            logger.error(
                'Request role {0} does not match server role {1}'.format(
                role, self.role))
            self._notify()
        else:
            # Should we check this for every request instead?
            if self.keep_open is None:
                self.keep_open = bool(FCGI_KEEP_CONN & flags)
            request = Request(self.conn, record.request_id, role)
            if role == FCGI_FILTER:
                request.data = InputStream()
            self.requests[request.id] = request

    def fcgi_params(self, record, request):
        request._environ.feed(record.content)
        if not record.content:
            request.environ = dict(unpack_pairs(request._environ.read()))
            del request._environ
            if request.role == FCGI_AUTHORIZER:
                request._greenlet = self._spawn(self._handle_request, request)

    def fcgi_abort_request(self, record, request):
        logger.warn('Aborting request {0}'.format(request.id))
        if request.id in self.requests:
            greenlet = request._greenlet
            if greenlet is None:
                self.fcgi_end_request(request)
                self._notify()
            else:
                logger.warn('Killing greenlet {0} for request {1}'.format(
                    greenlet, request.id))
                greenlet.kill()
                greenlet.join()
        else:
            logger.debug('Request {0} not found'.format(request.id))

    def fcgi_get_values(self, record):
        pairs = ((name, self.capabilities.get(name)) for name, _ in
                 unpack_pairs(record.content))
        content = pack_pairs(
            (name, str(value)) for name, value in pairs if value)
        self.send_record(FCGI_GET_VALUES_RESULT, content)
        self._notify()

    def fcgi_end_request(self, request, request_status=FCGI_REQUEST_COMPLETE,
                         app_status=0):
        self.send_record(FCGI_END_REQUEST, pack_end_request(
            app_status, request_status), request.id)
        del self.requests[request.id]
        logger.debug('Request {0} ended'.format(request.id))

    def run(self):
        reader = self._spawn(self._reader)
        while 1:
            self._event.wait()
            logger.debug('Some greenlet has finished its job')
            if self.requests:
                logger.debug('Connection left open due to active requests')
            elif self.keep_open and not reader.ready():
                logger.debug('Connection left open due to KEEP_CONN flag')
            else:
                break
            self._event.clear()

        logger.debug('Closing connection')
        # reader will stop too once we close connection
        self.conn.close()

    def _handle_request(self, request):
        try:
            self.request_handler(request)
            request.stdout.close()
            request.stderr.close()
        finally:
            self.fcgi_end_request(request)

    def _reader(self):
        for record in self.conn:
            if record.type in EXISTING_REQUEST_RECORD_TYPES:
                self._handle_request_record(record)
            elif record.type == FCGI_BEGIN_REQUEST:
                self.fcgi_begin_request(record)
            elif record.type == FCGI_GET_VALUES:
                self.fcgi_get_values(record)
            else:
                logger.error('{0}: Unknown record type'.format(record))
                self.send_record(FCGI_UNKNOWN_TYPE,
                                 pack_unknown_type(record.type))

    def _handle_request_record(self, record):
        request = self.requests.get(record.request_id)

        if not request:
            logger.error('{0} for non-existent request'.format(record))
        elif record.type == FCGI_STDIN:
            request.stdin.feed(record.content)
            if not record.content and request.role == FCGI_RESPONDER:
                request._greenlet = self._spawn(self._handle_request, request)
        elif record.type == FCGI_DATA:
            request.data.feed(record.content)
            if not record.content and request.role == FCGI_FILTER:
                request._greenlet = self._spawn(self._handle_request, request)
        elif record.type == FCGI_PARAMS:
            self.fcgi_params(record, request)
        elif record.type == FCGI_ABORT_REQUEST:
            self.fcgi_abort_request(record, request)

    def _spawn(self, callable, *args, **kwargs):
        g = spawn(callable, *args, **kwargs)
        g.link(self._notify)
        return g

    def _notify(self, source=None):
        self._event.set()


class FastCGIServer(StreamServer):
    """
    Server that handles communication with Web-server via FastCGI protocol.
    It is request_handler's responsibility to choose protocol and deal with
    application invocation. gevent_fastcgi.wsgi module contains WSGI
    protocol implementation.
    """

    def __init__(self, listener, request_handler, role=FCGI_RESPONDER,
                 num_workers=1, buffer_size=1024, max_conns=1024, **kwargs):
        # StreamServer does not create UNIX-sockets
        if isinstance(listener, basestring):
            address_family = socket.AF_UNIX
            self._unix_socket = listener
            listener = socket.socket(address_family, socket.SOCK_STREAM)

        super(FastCGIServer, self).__init__(
            listener, self.handle_connection, spawn=max_conns, **kwargs)

        if role not in (FCGI_RESPONDER, FCGI_FILTER, FCGI_AUTHORIZER):
            raise ValueError('Illegal FastCGI role {0}'.format(role))

        self.max_conns = max_conns
        self.role = role
        self.request_handler = request_handler
        self.buffer_size = buffer_size
        self.capabilities = dict(
            FCGI_MAX_CONNS=str(max_conns),
            FCGI_MAX_REQS=str(max_conns * 1024),
            FCGI_MPXS_CONNS='1',
        )

        self.num_workers = int(num_workers)
        assert self.num_workers > 0, 'num_workers must be positive number'
        self._workers = []

    def start(self):
        logger.debug('Starting server')
        if not self.started:
            if hasattr(self, '_unix_socket'):
                self.socket.bind(self._unix_socket)
                self.socket.listen(self.max_conns)

            super(FastCGIServer, self).start()

            if self.num_workers > 1:
                logger.debug('Forking {0} worker(s)'.format(self.num_workers))
                for _ in range(self.num_workers):
                    self._start_worker()

                self._supervisor = spawn(self._watch_workers)

                try:
                    self.socket.close()
                except:
                    self.kill()
                    raise

    def stop(self, timeout=None):
        super(FastCGIServer, self).stop(timeout)

        if self._workers is not None:
            # master process
            try:
                self._kill_workers()
            finally:
                if hasattr(self, '_unix_socket'):
                    try:
                        os.unlink(self._unix_socket)
                    except OSError:
                        logger.exception(
                            'Failed to remove socket file {0}'
                            .format(self.address))
        else:
            # worker
            os._exit(0)

    def start_accepting(self):
        # master proceess with workers is not allowed to accept
        if self._workers is None or self.num_workers == 1:
            super(FastCGIServer, self).start_accepting()

    def stop_accepting(self):
        # master proceess with workers is not allowed to accept
        if self._workers is None or self.num_workers == 1:
            super(FastCGIServer, self).stop_accepting()

    def handle_connection(self, sock, addr):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        conn = ServerConnection(sock, self.buffer_size)
        handler = ConnectionHandler(
            conn, self.role, self.capabilities, self.request_handler)
        handler.run()

    def _start_worker(self):
        pid = os.fork()
        if pid:
            # master process
            self._workers.append(pid)
            logger.debug('Started worker {0}'.format(pid))
            return pid
        else:  # pragma: nocover
            # worker should never return
            try:
                self._workers = None
                signal(SIGHUP, self.stop)
                self.start_accepting()
                super(FastCGIServer, self).serve_forever()
            finally:
                os._exit(0)

    def _kill_workers(self):

        def kill_seq(max_timeout):
            for sig in SIGHUP, SIGKILL:
                if not self._workers:
                    break
                logger.debug('Killing workers {0} with signal {1}'.
                             format(self._workers, sig))
                for pid in self._workers[:]:
                    yield pid, sig

                sleep(0)
                if self._workers:
                    sleep(max_timeout)

        for pid, sig in kill_seq(2):
            try:
                logger.debug(
                    'Killing worker {0} with signal {1}'.format(pid, sig))
                os.kill(pid, sig)
                sleep(0)
            except OSError, x:
                if x.errno == errno.ESRCH:
                    logger.error('Worker with pid {0} not found'.format(pid))
                    if pid in self._workers:
                        self._workers.remove(pid)
                    continue
                if x.errno == errno.ECHILD:
                    logger.error('No alive workers left')
                    self._workers = []
                    break
        if self._workers:
            logger.debug('There are still some alive workers after'
                         'attempting to kill them')

    def _watch_workers(self, check_interval=1):
        while True:
            sleep(check_interval)
            try:
                while 1:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                    if pid == 0:
                        break
                    if pid in self._workers:
                        logger.debug('Worker {0} exited'.format(pid))
                        self._workers.remove(pid)
            except OSError, e:
                if e.errno != errno.ECHILD:
                    logger.exception('Failed to check if any worker died')
                continue
