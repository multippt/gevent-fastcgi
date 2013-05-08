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
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import with_statement

import os
import sys
import logging
from errno import EPIPE, ECONNRESET
from tempfile import SpooledTemporaryFile

try:
    from cStringIO import StringIO
except ImportError:  # pragma: no cover
    from StringIO import StringIO

from zope.interface import implements
from gevent import socket
from gevent.event import Event

from gevent_fastcgi.interfaces import IConnection
from gevent_fastcgi.const import (
    FCGI_VERSION,
    FCGI_STDIN,
    FCGI_STDOUT,
    FCGI_STDERR,
    FCGI_DATA,
    FCGI_NULL_REQUEST_ID,
    FCGI_RECORD_HEADER_LEN,
    FCGI_RECORD_TYPES,
    begin_request_struct,
    end_request_struct,
    header_struct,
    unknown_type_struct,
)

logger = logging.getLogger(__name__)


class Record(object):

    __slots__ = ('type', 'content', 'request_id')

    def __init__(self, type, content='', request_id=FCGI_NULL_REQUEST_ID):
        if type < 0 or type > 255:
            raise ValueError('Record type must be between 0 and 255')
        self.type = type
        self.content = content
        self.request_id = request_id

    def __str__(self):
        return '<Record %s, req id %s, %d bytes>' % (
            FCGI_RECORD_TYPES.get(self.type, self.type),
            self.request_id,
            len(self.content))


class Connection(object):

    implements(IConnection)

    def __init__(self, sock, buffer_size=4096):
        self._sock = sock
        self.buffered_reader = BufferedReader(sock.recv, buffer_size)

    def write_record(self, record):
        logger.debug('Sending %s' % record)
        sendall = self._sock.sendall
        content_len = len(record.content)
        if content_len <= 0xffff:
            header = header_struct.pack(
                FCGI_VERSION, record.type, record.request_id, content_len, 0)
            sendall(header + record.content)
        elif record.type in (FCGI_STDIN, FCGI_STDOUT, FCGI_STDERR, FCGI_DATA):
            sent = 0
            content = record.content
            while sent < content_len:
                chunk_len = min(0xfff8, content_len - sent)
                header = header_struct.pack(
                    FCGI_VERSION, record.type, record.request_id, chunk_len, 0)
                sendall(header + content[sent:sent+chunk_len])
                sent += chunk_len
        else:
            msg = 'Record content length %s exceeds maximum of %d' % (
                content_len, 0xffff)
            logger.error(msg)
            raise ValueError(msg)

    def read_record(self):
        read_bytes = self.buffered_reader.read_bytes

        try:
            header = read_bytes(FCGI_RECORD_HEADER_LEN)
        except PartialRead, x:
            if x.partial_data:
                logger.exception('Partial header received: %s' % x)
                raise
            # Remote side closed connection after sending all records
            return None

        version, record_type, request_id, content_len, padding = \
            header_struct.unpack_from(header)

        if content_len:
            content = read_bytes(content_len)
        else:
            content = ''

        if padding:  # pragma: no cover
            read_bytes(padding)

        record = Record(record_type, content, request_id)
        logger.debug('Received %s' % record)

        return record

    def __iter__(self):
        return iter(self.read_record, None)

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None

    def done_writing(self):
        self._sock.shutdown(socket.SHUT_WR)


class InputStream(object):
    """
    FCGI_STDIN or FCGI_DATA stream.
    Uses temporary file to store received data once max_mem bytes
    have been received.
    """
    def __init__(self, max_mem=1024):
        self._file = SpooledTemporaryFile(max_mem)
        self._eof_received = Event()

    def feed(self, data):
        if self.eof_received:
            raise IOError('Feeding file beyond EOF mark')
        if not data:  # EOF mark
            self._file.seek(0)
            self._eof_received.set()
            return
        self._file.write(data)

    def __iter__(self):
        self._eof_received.wait()
        return iter(self._file)

    def read(self, size=-1):
        self._eof_received.wait()
        return self._file.read(size)

    def readlines(self, sizehint=0):
        self._eof_received.wait()
        return self._file.readlines(sizehint)

    @property
    def eof_received(self):
        return self._eof_received.is_set()


class OutputStream(object):
    """
    FCGI_STDOUT or FCGI_STDERR stream.
    """
    def __init__(self, conn, request_id, record_type):
        self.conn = conn
        self.request_id = request_id
        self.record_type = record_type
        self.closed = False

    def write(self, data):
        if self.closed:
            raise ValueError('Write on closed OutputStream')

        if not data:
            return

        if self.record_type == FCGI_STDERR:
            sys.stderr.write(data)

        self.conn.write_record(Record(self.record_type, data, self.request_id))

    def writelines(self, lines):
        map(self.write, lines)

    def flush(self):
        pass

    def close(self):
        if not self.closed:
            self.conn.write_record(
                Record(self.record_type, '', self.request_id))
            self.closed = True
            logger.debug('Closing output stream')


class Request(object):

    def __init__(self, conn, request_id, role):
        self.conn = conn
        self.id = request_id
        self.role = role
        self.stdin = InputStream()
        self.stdout = OutputStream(conn, request_id, FCGI_STDOUT)
        self.stderr = OutputStream(conn, request_id, FCGI_STDERR)
        self.greenlet = None
        self.environ_list = []
        self.environ = {}
        self.status = None
        self.headers = None
        self.headers_sent = False


try:
    from gevent_fastcgi.speedups import pack_pair, unpack_pairs
except ImportError:  # pragma: no cover
    import struct

    length_struct = struct.Struct('!L')

    def pack_len(s):
        l = len(s)
        if l < 128:
            return chr(l)
        elif l > 0x7fffffff:
            raise ValueError('Maximum name or value length is %d', 0x7fffffff)
        return length_struct.pack(l | 0x80000000)

    def pack_pair(name, value):
        return ''.join((pack_len(name), pack_len(value), name, value))

    def unpack_len(buf, pos):
        _len = ord(buf[pos])
        if _len & 128:
            _len = length_struct.unpack_from(buf, pos)[0] & 0x7fffffff
            pos += 4
        else:
            pos += 1
        return _len, pos

    def unpack_pairs(data):
        end = len(data)
        pos = 0
        while pos < end:
            try:
                name_len, pos = unpack_len(data, pos)
                value_len, pos = unpack_len(data, pos)
                name = data[pos:pos + name_len]
                pos += name_len
                value = data[pos:pos + value_len]
                pos += value_len
                yield name, value
            except (IndexError, struct.error):
                raise ValueError('Failed to unpack name/value pairs')


def pack_pairs(pairs):
    if isinstance(pairs, dict):
        pairs = pairs.iteritems()

    return ''.join(pack_pair(name, value) for name, value in pairs)


class PartialRead(Exception):
    """ Raised by buffered_reader when it fails to read requested length
    of data
    """
    def __init__(self, requested_size, partial_data):
        super(PartialRead, self).__init__(
            'Expected %s but received %s bytes only' % (requested_size,
            len(partial_data)))
        self.requested_size = requested_size
        self.partial_data = partial_data


class BufferedReader(object):
    """ Allows to receive data in large chunks
    """
    def __init__(self, read_callable, buffer_size):
        self._reader = _reader_generator(read_callable, buffer_size)
        self.read_bytes = self._reader.send
        self._reader.next()  # advance generator to first yield statement


def _reader_generator(read, buf_size):
    buf = ''
    blen = 0
    chunks = []
    size = (yield)

    while True:
        if blen >= size:
            data, buf = buf[:size], buf[size:]
            blen -= size
        else:
            while blen < size:
                chunks.append(buf)
                buf = read((size - blen + buf_size - 1) / buf_size * buf_size)
                if not buf:
                    raise PartialRead(size, ''.join(chunks))
                blen += len(buf)

            blen -= size

            if blen:
                chunks.append(buf[:-blen])
                buf = buf[-blen:]
            else:
                chunks.append(buf)
                buf = ''

            data = ''.join(chunks)
            chunks = []

        size = (yield data)
