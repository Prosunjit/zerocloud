from copy import deepcopy
import ctypes
import re
import struct
import traceback
import time
import datetime
import uuid
from hashlib import md5
from random import randrange
import greenlet
from eventlet import GreenPile, GreenPool, Queue, spawn_n
from eventlet.green import socket
from eventlet.timeout import Timeout
import zlib

from swiftclient.client import quote

from swift.common.http import HTTP_CONTINUE, is_success, \
    HTTP_INSUFFICIENT_STORAGE, is_client_error, HTTP_NOT_FOUND
from swift.proxy.controllers.base import update_headers, delay_denial, \
    cors_validation, get_info, close_swift_conn
from swift.common.utils import split_path, get_logger, TRUE_VALUES, \
    get_remote_client, ContextPool, cache_from_env, normalize_timestamp, \
    GreenthreadSafeIterator
from swift.proxy.server import ObjectController, ContainerController, \
    AccountController
from swift.common.bufferedhttp import http_connect
from swift.common.exceptions import ConnectionTimeout, ChunkReadTimeout
from swift.common.constraints import check_utf8, MAX_FILE_SIZE
from swift.common.swob import Request, Response, HTTPNotFound, \
    HTTPPreconditionFailed, HTTPRequestTimeout, HTTPRequestEntityTooLarge, \
    HTTPBadRequest, HTTPUnprocessableEntity, HTTPServiceUnavailable, \
    HTTPClientDisconnect, wsgify
from zerocloud.common import ACCESS_READABLE, ACCESS_CDR, \
    CLUSTER_CONFIG_FILENAME, NODE_CONFIG_FILENAME, TAR_MIMES, \
    POST_TEXT_OBJECT_SYSTEM_MAP, POST_TEXT_ACCOUNT_SYSTEM_MAP, \
    merge_headers, update_metadata, DEFAULT_EXE_SYSTEM_MAP, \
    STREAM_CACHE_SIZE, ZvmChannel, parse_location, is_swift_path, \
    is_image_path, can_run_as_daemon, SwiftPath, load_server_conf
from zerocloud.configparser import ClusterConfigParser, \
    ClusterConfigParsingError
from zerocloud.tarstream import StringBuffer, UntarStream, \
    TarStream, REGTYPE, BLOCKSIZE, NUL, ExtractedFile, Path, ReadError
from zerocloud.thread_pool import Zuid



def my_debug(str, arg):
	print "------------------{}----------------------".format(str)
	print arg
	print "------------------{}----------------------".format(str)

try:
    import simplejson as json
except ImportError:
    import json



'''
	cluster configuration comes from the .json file.
	code review comment: 
	__call__ --> ... 
	
	--> handler = getattr(controller, req.method): call req.method=POST

	--> hander(req) : This calls the POST method of ClusterController object.

	--> POST():
		if content_type == json, read .json file, which is used as cluster configuration file.
		.json file is then parsed and self.parser object contains #zvm object/nodes in the node_list variable of self.parser object.

		make_exec_request()
		_process_response() : get response from a given connection node.
	
	
	--> _process_response()
		process_server_response(): 

	--> process_server_response() 
		ObjectController(...).PUT()
'''

# Monkey patching Request to support content_type property properly
def _req_content_type_property():
    """
    Set and retrieve Request.content_type
    Strips off any charset when retrieved
    """
    def getter(self):
        if 'content-type' in self.headers:
            return self.headers.get('content-type').split(';')[0]

    def setter(self, value):
        self.headers['content-type'] = value

    return property(getter, setter,
                    doc="Retrieve and set the request Content-Type header")

Request.content_type = _req_content_type_property()


class GreenPileEx(GreenPile):
    def __init__(self, size_or_pool=1000):
        super(GreenPileEx, self).__init__(size_or_pool)
        self.current = None

    def next(self):
        """Wait for the next result, suspending the current greenthread until it
        is available.  Raises StopIteration when there are no more results."""
        if self.counter == 0 and self.used:
            raise StopIteration()
        try:
            if not self.current:
                self.current = self.waiters.get()
            res = self.current.wait()
            self.current = None
            return res
        finally:
            if not self.current:
                self.counter -= 1


class CachedBody(object):

    def __init__(self, read_iter, cache=None, cache_size=STREAM_CACHE_SIZE,
                 total_size=None):
        self.read_iter = read_iter
        self.total_size = total_size
        if cache:
            self.cache = cache
        else:
            self.cache = []
            size = 0
            for chunk in read_iter:
                self.cache.append(chunk)
                size += len(chunk)
                if size >= cache_size:
                    break

    def __iter__(self):
        if self.total_size:
            for chunk in self.cache:
                self.total_size -= len(chunk)
                if self.total_size < 0:
                    yield chunk[:self.total_size]
                    break
                else:
                    yield chunk
            if self.total_size > 0:
                for chunk in self.read_iter:
                    self.total_size -= len(chunk)
                    if self.total_size < 0:
                        yield chunk[:self.total_size]
                        break
                    else:
                        yield chunk
            for _junk in self.read_iter:
                pass
        else:
            for chunk in self.cache:
                yield chunk
            for chunk in self.read_iter:
                yield chunk


class FinalBody(object):

    def __init__(self, app_iter):
        self.app_iters = [app_iter]

    def __iter__(self):
        for app_iter in self.app_iters:
            for chunk in app_iter:
                yield chunk

    def append(self, app_iter):
        self.app_iters.append(app_iter)


class NameService(object):

    INT_FMT = '!I'
    INPUT_RECORD_FMT = '!IH'
    OUTPUT_RECORD_FMT = '!4sH'
    INT_SIZE = struct.calcsize(INT_FMT)
    INPUT_RECORD_SIZE = struct.calcsize(INPUT_RECORD_FMT)
    OUTPUT_RECORD_SIZE = struct.calcsize(OUTPUT_RECORD_FMT)

    def __init__(self, peers):
        self.port = None
        self.hostaddr = None
        self.peers = peers
        self.sock = None
        self.thread = None
        self.bind_map = {}
        self.conn_map = {}
        self.peer_map = {}
        self.int_pool = GreenPool()

    def start(self, pool):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('', 0))
        self.thread = pool.spawn(self._run)
        (self.hostaddr, self.port) = self.sock.getsockname()

    def _run(self):
        while 1:
            try:
                start = time.time()
                message, peer_address = self.sock.recvfrom(65535)
                offset = 0
                peer_id = struct.unpack_from(NameService.INT_FMT, message,
                                             offset)[0]
                offset += NameService.INT_SIZE
                bind_count = struct.unpack_from(NameService.INT_FMT, message,
                                                offset)[0]
                offset += NameService.INT_SIZE
                connect_count = struct.unpack_from(NameService.INT_FMT,
                                                   message, offset)[0]
                offset += NameService.INT_SIZE
                for i in range(bind_count):
                    connecting_host, port = struct.unpack_from(
                        NameService.INPUT_RECORD_FMT, message, offset)[0:2]
                    offset += NameService.INPUT_RECORD_SIZE
                    self.bind_map.setdefault(peer_id, {})[connecting_host] = \
                        port
                self.conn_map[peer_id] = (connect_count,
                                          offset,
                                          ctypes.create_string_buffer(
                                              message[:]))
                self.peer_map.setdefault(peer_id, {})[0] = peer_address[0]
                self.peer_map.setdefault(peer_id, {})[1] = peer_address[1]

                if len(self.peer_map) == self.peers:
                    print "Finished name server receive in %.3f seconds" \
                          % (time.time() - start)
                    start = time.time()
                    for peer_id in self.peer_map.iterkeys():
                        (connect_count, offset, reply) = self.conn_map[peer_id]
                        for i in range(connect_count):
                            connecting_host = struct.unpack_from(
                                NameService.INT_FMT, reply, offset)[0]
                            port = self.bind_map[connecting_host][peer_id]
                            connect_to = self.peer_map[connecting_host][0]
                            if connect_to == self.peer_map[peer_id][0]:
                                # both on the same host
                                connect_to = '127.0.0.1'
                            struct.pack_into(NameService.OUTPUT_RECORD_FMT,
                                             reply, offset,
                                             socket.inet_pton(socket.AF_INET,
                                                              connect_to),
                                             port)
                            offset += NameService.OUTPUT_RECORD_SIZE
                        self.sock.sendto(reply, (self.peer_map[peer_id][0],
                                                 self.peer_map[peer_id][1]))
                    print "Finished name server send in %.3f seconds" \
                          % (time.time() - start)
            except greenlet.GreenletExit:
                return
            except Exception:
                print traceback.format_exc()
                pass

    def stop(self):
        self.thread.kill()
        self.sock.close()


class ProxyQueryMiddleware(object):

    def list_account(self, account, mask=None, marker=None, request=None):
        new_req = request.copy_get()
        new_req.path_info = '/' + quote(account)
        new_req.query_string = 'format=json'
        if marker:
            new_req.query_string += '&marker=' + marker
        resp = AccountController(self.app, account).GET(new_req)
        if resp.status_int == 204:
            data = resp.body
            return []
        if resp.status_int < 200 or resp.status_int >= 300:
            raise Exception('Error querying object server')
        data = json.loads(resp.body)
        if marker:
            return data
        ret = []
        while data:
            for item in data:
                if not mask or mask.match(item['name']):
                    ret.append(item['name'])
            marker = data[-1]['name']
            data = self.list_account(account, mask=None, marker=marker,
                                     request=request)
        return ret

    def list_container(self, account, container, mask=None, marker=None,
                       request=None):
        new_req = request.copy_get()
        new_req.path_info = '/' + quote(account) + '/' + quote(container)
        new_req.query_string = 'format=json'
        if marker:
            new_req.query_string += '&marker=' + marker
        resp = ContainerController(self.app, account, container).GET(new_req)
        if resp.status_int == 204:
            data = resp.body
            return []
        if resp.status_int < 200 or resp.status_int >= 300:
            raise Exception('Error querying object server')
        data = json.loads(resp.body)
        if marker:
            return data
        ret = []
        while data:
            for item in data:
                if item['name'][-1] == '/':
                    continue
                if not mask or mask.match(item['name']):
                    ret.append(item['name'])
            marker = data[-1]['name']
            data = self.list_container(account, container,
                                       mask=None, marker=marker,
                                       request=request)
        return ret

    def parse_daemon_config(self, daemon_list):
        result = []
        request = Request.blank('/daemon', environ={'REQUEST_METHOD': 'POST'},
                                headers={'Content-Type': 'application/json'})
        socks = {}
        for sock, conf_file in zip(*[iter(daemon_list)] * 2):
            if socks.get(sock, None):
                self.logger.warning('Duplicate daemon config for uuid %s'
                                    % sock)
                continue
            socks[sock] = 1
            try:
                json_config = json.load(open(conf_file))
            except IOError:
                self.logger.warning('Cannot load daemon config file: %s'
                                    % conf_file)
                continue
            parser = ClusterConfigParser(self.zerovm_sysimage_devices,
                                         self.zerovm_content_type,
                                         self.parser_config,
                                         self.list_account,
                                         self.list_container,
                                         network_type=self.network_type)
            try:
                parser.parse(json_config, False, request=request)
            except ClusterConfigParsingError, e:
                self.logger.warning('Daemon config %s error: %s'
                                    % (conf_file, str(e)))
                continue
            if len(parser.nodes) != 1:
                self.logger.warning('Bad daemon config %s: too many nodes'
                                    % conf_file)
            for node in parser.nodes.itervalues():
                if node.bind or node.connect:
                    self.logger.warning('Bad daemon config %s: '
                                        'network channels are present'
                                        % conf_file)
                    continue
                if not is_image_path(node.exe):
                    self.logger.warning('Bad daemon config %s: '
                                        'exe path must be in image file'
                                        % conf_file)
                    continue
                image = None
                for sysimage in parser.sysimage_devices.keys():
                    if node.exe.image == sysimage:
                        image = sysimage
                        break
                if not image:
                    self.logger.warning('Bad daemon config %s: '
                                        'exe is not in sysimage device'
                                        % conf_file)
                    continue
                node.channels = sorted(node.channels, key=lambda ch: ch.device)
                result.append((sock, node))
                self.logger.info('Loaded daemon config %s with UUID %s'
                                 % (conf_file, sock))
        return result

    def __init__(self, app, conf, logger=None,
                 object_ring=None, container_ring=None):
        self.app = app
        if logger:
            self.logger = logger
        else:
            self.logger = get_logger(conf, log_route='proxy-query')
        # let's load appropriate server config sections here
        load_server_conf(conf, ['app:proxy-server'])
        self.node_timeout = int(conf.get('node_timeout', 10))
        self.immediate_response_timeout = \
            int(conf.get('interactive_timeout', self.node_timeout))
        self.ignore_replication = conf.get(
            'zerovm_ignore_replication', 'f').lower() in TRUE_VALUES
        # network chunk size for all network ops
        self.network_chunk_size = int(conf.get('network_chunk_size',
                                               65536))
        # max time to wait for upload to finish, used in POST requests
        self.max_upload_time = int(conf.get('max_upload_time', 86400))
        self.client_timeout = int(conf.get('client_timeout', 60))
        self.put_queue_depth = int(conf.get('put_queue_depth', 10))
        self.conn_timeout = float(conf.get('conn_timeout', 0.5))
        # header for "execute by POST"
        self.zerovm_execute = 'x-zerovm-execute'
        # execution engine version
        self.zerovm_execute_ver = '1.0'
        # maximum size of a system map file
        self.zerovm_maxconfig = int(conf.get('zerovm_maxconfig', 65536))
        # name server hostname or ip, will be autodetected if not set
        self.zerovm_ns_hostname = conf.get('zerovm_ns_hostname')
        # name server thread pool size
        self.zerovm_ns_maxpool = int(conf.get('zerovm_ns_maxpool', 1000))
        self.zerovm_ns_thrdpool = GreenPool(self.zerovm_ns_maxpool)
        # use newest files when running zerovm executables, default - False
        self.zerovm_uses_newest = conf.get(
            'zerovm_uses_newest', 'f').lower() in TRUE_VALUES
        # use executable validation info, stored on PUT or POST,
        # to shave some time on zerovm startup
        self.zerovm_prevalidate = conf.get(
            'zerovm_prevalidate', 'f').lower() in TRUE_VALUES
        # use CORS workaround to POST execute commands, default - False
        self.zerovm_use_cors = conf.get(
            'zerovm_use_cors', 'f').lower() in TRUE_VALUES
        # Accounting: enable or disabe execution accounting data,
        # default - disabled
        self.zerovm_accounting_enabled = conf.get(
            'zerovm_accounting_enabled', 'f').lower() in TRUE_VALUES
        # Accounting: system account for storing accounting data
        self.cdr_account = conf.get('user_stats_account', 'userstats')
        # Accounting: storage API version
        self.version = 'v1'
        # default content-type for unknown files
        self.zerovm_content_type = conf.get(
            'zerovm_default_content_type', 'application/octet-stream')
        # names of sysimage devices, no sysimage devices exist by default
        devs = [(i.strip(), None)
                for i in conf.get('zerovm_sysimage_devices', '').split()
                if i.strip()]
        self.zerovm_sysimage_devices = dict(devs)
        # GET support: container for content-type association storage
        self.zerovm_registry_path = '.zvm'
        # GET support: API version for "open" command
        self.zerovm_open_version = 'open'
        # GET support: API version for "open with" command
        self.zerovm_openwith_version = 'open-with'
        # GET support: allowed commands
        self.zerovm_allowed_commands = [self.zerovm_open_version,
                                        self.zerovm_openwith_version]
        # GET support: cache config files for this amount of seconds
        self.zerovm_cache_config_timeout = 60
        self.parser_config = {
            'limits': {
                # total maximum iops for channel read or write operations
                # per zerovm session
                'reads': int(conf.get('zerovm_maxiops', 1024 * 1048576)),
                'writes': int(conf.get('zerovm_maxiops', 1024 * 1048576)),
                # total maximum bytes for a channel write operations
                # per zerovm session
                'rbytes': int(conf.get('zerovm_maxoutput', 1024 * 1048576)),
                # total maximum bytes for a channel read operations
                # per zerovm session
                'wbytes': int(conf.get('zerovm_maxinput', 1024 * 1048576))
            }
        }
        # use direct tcp connections (tcp) or intermediate broker (opaque)
        self.network_type = conf.get('zerovm_network_type', 'tcp')
        # opaque network does not support replication right now
        if self.network_type == 'opaque':
            self.ignore_replication = True
        # list of daemons we need to lazy load
        # (first request will start the daemon)
        daemon_list = [i.strip() for i in
                       conf.get('zerovm_daemons', '').split() if i.strip()]
        self.zerovm_daemons = self.parse_daemon_config(daemon_list)
        self.uid_generator = Zuid()

    @wsgify
    def __call__(self, req):
    	my_debug("#__call__", req.__dict__)
	my_debug("req.path", req.path)

        try:
            version, account, container, obj = split_path(req.path, 1, 4, True)
            path_parts = dict(version=version,
                              account_name=account,
                              container_name=container,
                              object_name=obj)
        except ValueError:
            return HTTPNotFound(request=req)
        if account and \
                (self.zerovm_execute in req.headers
                 or version in self.zerovm_allowed_commands):
            if req.content_length and req.content_length < 0:
                return HTTPBadRequest(request=req,
                                      body='Invalid Content-Length')
            if not check_utf8(req.path_info):
                return HTTPPreconditionFailed(request=req, body='Invalid UTF8')
            controller = self.get_controller(account, container, obj)
            if not controller:
                return HTTPPreconditionFailed(request=req, body='Bad URL')
            if 'swift.trans_id' not in req.environ:
                # if this wasn't set by an earlier middleware, set it now
                trans_id = 'tx' + uuid.uuid4().hex
                req.environ['swift.trans_id'] = trans_id
                self.logger.txn_id = trans_id
            req.headers['x-trans-id'] = req.environ['swift.trans_id']
            controller.trans_id = req.environ['swift.trans_id']
            self.logger.client_ip = get_remote_client(req)
            if path_parts['version']:
                controller.command = path_parts['version']
                req.path_info_pop()
            if self.zerovm_execute not in req.headers:
                req.headers[self.zerovm_execute] = \
                    self.zerovm_execute_ver
            try:
                handler = getattr(controller, req.method)
            except AttributeError:
                return HTTPPreconditionFailed(request=req,
                                              body='Bad HTTP method')
            # we do not deny access based on user account permissions
            # as users can allow other users to run their code and be billed
            # accordingly
            # more than that, accounts can allow anonymous people to run
            # executables, and these accounts will be billed for that
            # if 'swift.authorize' in req.environ:
            #     resp = req.environ['swift.authorize'](req)
            #     if not resp:
            #         del req.environ['swift.authorize']
            #     else:
            #         if not getattr(handler, 'delay_denial', None):
            #             return resp
            start_time = time.time()
            # each request is assigned a unique k-sorted id
            # it will be used by QoS code to assign slots/priority
            req.headers['x-zerocloud-id'] = self.uid_generator.get()
            res = handler(req)
            perf = time.time() - start_time
            if 'x-nexe-cdr-line' in res.headers:
                res.headers['x-nexe-cdr-line'] = \
                    '%.3f, %s' % (perf, res.headers['x-nexe-cdr-line'])
            return res
        return self.app

    def get_controller(self, account, container, obj):
        return ClusterController(self.app, account, container, obj, self)


class ClusterController(ObjectController):

    def __init__(self, app, account_name, container_name, obj_name, middleware,
                 **kwargs):
        ObjectController.__init__(self, app,
                                  account_name,
                                  container_name or '',
                                  obj_name or '')
        self.middleware = middleware
        self.command = None
        self.parser = ClusterConfigParser(
            self.middleware.zerovm_sysimage_devices,
            self.middleware.zerovm_content_type,
            self.middleware.parser_config,
            self.middleware.list_account,
            self.middleware.list_container,
            network_type=self.middleware.network_type)

    def get_daemon_socket(self, config):
        for daemon_sock, daemon_conf in self.middleware.zerovm_daemons:
            if can_run_as_daemon(config, daemon_conf):
                return daemon_sock
        return None

    def get_random_partition(self):
        partition_count = self.app.object_ring.partition_count
        part = randrange(0, partition_count)
        return part

    def _get_own_address(self):
        if self.middleware.zerovm_ns_hostname:
            addr = self.middleware.zerovm_ns_hostname
        else:
            addr = None
            partition_count = self.app.object_ring.partition_count
            part = randrange(0, partition_count)
            nodes = self.app.object_ring.get_part_nodes(part)
            for n in nodes:
                addr = _get_local_address(n)
                if addr:
                    break
        return addr

    def _make_exec_requests(self, pile, exec_requests):
    	my_debug("inside _make_exec_requests", exec_requests)
	my_debug("pile", pile.__dict__)
        for exec_request in exec_requests:
            node = exec_request.node
	    '''
		node object here contains all the required information required for a to be created zerovm node.
		this is some of the members of node:
		'args': 'hello.pybangladesh',
		'channels': [
		            <zerocloud.common.ZvmChannelobjectat0x309f190>,
			    <zerocloud.common.ZvmChannelobjectat0x309f1d0>,
			    <zerocloud.common.ZvmChannelobjectat0x309f210>
			],
		'access': 'GET',
		'exe': <zerocloud.common.ImagePathinstanceat0x32d04d0>,
		'name': 'hello',
		...
		and so on.

	    '''
	    my_debug("node", node.__dict__)

	    my_debug("node.exe.__dict__", node.exe.__dict__)
	    my_debug("node.channel[0].__dict__",node.channels[0].__dict__)
	    my_debug("node.channel[0].path.__dict__",node.channels[0].path.__dict__)

	    #node.path_info contains '/AUTH_0548686192274465bcdc4acec67f4396/container1/hello.zapp'
            account, container, obj = \
                split_path(node.path_info, 1, 3, True)
            if obj:
                ring = self.app.object_ring
                partition = ring.get_part(account, container, obj)
                node_iter = GreenthreadSafeIterator(
                    self.iter_nodes_local_first(self.app.object_ring,
                                                partition))
            elif container:
                ring = self.app.container_ring
                partition = ring.get_part(account, container)
                node_iter = GreenthreadSafeIterator(
                    self.app.iter_nodes(ring, partition))
            else:
                partition = self.get_random_partition()
                node_iter = GreenthreadSafeIterator(
                    self.iter_nodes_local_first(
                        self.app.object_ring,
                        partition))

	    '''
		node.path_info contains '/AUTH_0548686192274465bcdc4acec67f4396/container1/hello.zapp'
	    '''
            exec_request.path_info = node.path_info
            exec_request.headers['x-zerovm-access'] = node.access
            if node.replicate > 1:
                container_info = self.container_info(account, container)
                container_partition = container_info['partition']
                containers = container_info['nodes']
                exec_headers = self._backend_requests(exec_request,
                                                      node.replicate,
                                                      container_partition,
                                                      containers)
                if node.skip_validation:
                    for hdr in exec_headers:
                        hdr['x-zerovm-valid'] = 'true'
                i = 0
                pile.spawn(self._connect_exec_node,
                           node_iter,
                           partition,
                           exec_request,
                           self.app.logger.thread_locals,
                           node,
                           exec_headers[i])
                for repl_node in node.replicas:
                    i += 1
                    pile.spawn(self._connect_exec_node,
                               node_iter,
                               partition,
                               exec_request,
                               self.app.logger.thread_locals,
                               repl_node,
                               exec_headers[i])
            else:
                if node.skip_validation:
                    exec_request.headers['x-zerovm-valid'] = 'true'
                pile.spawn(self._connect_exec_node,
                           node_iter,
                           partition,
                           exec_request,
                           self.app.logger.thread_locals,
                           node,
                           exec_request.headers)
        return [conn for conn in pile if conn]

    def _spawn_file_senders(self, conns, pool, req):
        for conn in conns:
            conn.failed = False
            conn.queue = Queue(self.middleware.put_queue_depth)
            conn.tar_stream = TarStream()
            pool.spawn(self._send_file, conn, req.path)

    def _get_remote_objects(self, node):
        channels = []
        if is_swift_path(node.exe):
            channels.append(ZvmChannel('boot', None, path=node.exe))
        for ch in node.channels:
            if is_swift_path(ch.path) \
                    and (ch.access & (ACCESS_READABLE | ACCESS_CDR)) \
                    and not self.parser.is_sysimage_device(ch.device) \
                    and ch.path.path != getattr(node, 'path_info', None):
                channels.append(ch)
        return channels

    def _create_request_for_remote_object(self, data_sources, channel,
                                          exe_resp, req, nexe_headers, node):
        source_resp = None
        load_from = channel.path.path
        if is_swift_path(channel.path) and not channel.path.obj:
            return HTTPBadRequest(request=req,
                                  body='Cannot use container %s as a remote '
                                       'object reference' % load_from)
        for resp in data_sources:
            if resp.request and load_from == resp.request.path_info:
                source_resp = resp
                break
        if not source_resp:
            if exe_resp and load_from == exe_resp.request.path_info:
                source_resp = exe_resp
            else:
                source_req = req.copy_get()
                source_req.path_info = load_from
                if self.middleware.zerovm_uses_newest:
                    source_req.headers['X-Newest'] = 'true'
                if self.middleware.zerovm_prevalidate \
                        and 'boot' in channel.device:
                    source_req.headers['X-Zerovm-Valid'] = 'true'
                acct, src_container_name, src_obj_name =\
                    split_path(load_from, 1, 3, True)
                container_info = self.container_info(acct, src_container_name)
                source_req.acl = container_info['read_acl']
                # left here for exec_acl support
                # if 'boot' in ch.device:
                #     source_req.acl = container_info['exec_acl']
                source_resp = \
                    ObjectController(self.app,
                                     acct,
                                     src_container_name,
                                     src_obj_name).GET(source_req)
                if source_resp.status_int >= 300:
                    update_headers(source_resp, nexe_headers)
                    source_resp.body = 'Error %s while fetching %s' \
                                       % (source_resp.status,
                                          source_req.path_info)
                    return source_resp
            source_resp.nodes = []
            data_sources.append(source_resp)
        node.last_data = source_resp
        source_resp.nodes.append({'node': node, 'dev': channel.device})
        if source_resp.headers.get('x-zerovm-valid', None) \
                and 'boot' in channel.device:
            node.skip_validation = True
        for repl_node in node.replicas:
            repl_node.last_data = source_resp
            source_resp.nodes.append({'node': repl_node,
                                      'dev': channel.device})

    def create_final_response(self, conns, req):
        final_body = None
        final_response = Response(request=req)
        req.cdr_log = []
        for conn in conns:
            resp = conn.resp
            got_nexe_header = False
            if resp:
                for key in conn.nexe_headers.keys():
                    if resp.headers.get(key):
                        got_nexe_header = True
                        conn.nexe_headers[key] = resp.headers.get(key)
            if conn.error:
                conn.nexe_headers['x-nexe-error'] = \
                    conn.error.replace('\n', '')
            else:
                if is_success(resp.status_int) and not got_nexe_header:
                    # looks like the middleware is not installed
                    # or not functioning otherwise we should get something
                    return HTTPServiceUnavailable(
                        request=req,
                        headers=resp.headers,
                        body='objectquery middleware is not installed '
                             'or not functioning')
            self._store_accounting_data(req, conn)
            merge_headers(final_response.headers, conn.nexe_headers)
            if resp and resp.headers.get('x-zerovm-daemon', None):
                final_response.headers['x-nexe-cached'] = 'true'
            if resp and resp.content_length > 0:
                if not resp.app_iter:
                    resp.app_iter = [resp.body]
                if final_body:
                    final_body.append(resp.app_iter)
                    final_response.content_length += resp.content_length
                else:
                    final_body = FinalBody(resp.app_iter)
                    final_response.app_iter = final_body
                    final_response.content_length = resp.content_length
                    final_response.content_type = resp.headers['content-type']
        if self.middleware.zerovm_accounting_enabled:
            self.middleware.zerovm_ns_thrdpool.spawn_n(
                self._store_accounting_data,
                req)
        if self.middleware.zerovm_use_cors and self.container_name:
            container_info = self.container_info(self.account_name,
                                                 self.container_name)
            if container_info.get('cors', None):
                if container_info['cors'].get('allow_origin', None):
                    final_response.headers['access-control-allow-origin'] = \
                        container_info['cors']['allow_origin']
                if container_info['cors'].get('expose_headers', None):
                    final_response.headers['access-control-expose-headers'] = \
                        container_info['cors']['expose_headers']
        etag = md5(str(time.time()))
        final_response.headers['Etag'] = etag.hexdigest()
        return final_response

    @delay_denial
    @cors_validation
    def POST(self, req, exe_resp=None, cluster_config=''):
    	my_debug("@POST", req.__dict__)
        image_resp = None
        user_image = False
        chunk_size = self.middleware.network_chunk_size
        if 'content-type' not in req.headers:
            return HTTPBadRequest(request=req,
                                  body='Must specify Content-Type')
        upload_expiration = time.time() + self.middleware.max_upload_time
        etag = md5()
        req.bytes_transferred = 0
        path_list = [StringBuffer(CLUSTER_CONFIG_FILENAME),
                     StringBuffer(NODE_CONFIG_FILENAME)]
        rdata = req.environ['wsgi.input']
        read_iter = iter(lambda: rdata.read(chunk_size), '')
        orig_content_type = req.content_type
        if req.content_type in ['application/x-gzip']:
            orig_iter = read_iter
            read_iter = gunzip_iter(orig_iter,
                                    chunk_size)
            req.content_type = TAR_MIMES[0]
        if req.content_type in TAR_MIMES:
            # we must have Content-Length set for tar-based requests
            # as it will be impossible to stream them otherwise
            if 'content-length' not in req.headers:
                return HTTPBadRequest(request=req,
                                      body='Must specify Content-Length')
            # buffer first blocks of tar file and search for the system map
            try:
                cached_body = CachedBody(read_iter)
            except zlib.error:
                return HTTPUnprocessableEntity(request=req,
                                               body='Error reading %s stream'
                                                    % orig_content_type)
            user_image = True
            image_resp = Response(
                app_iter=iter(cached_body),
                headers={
                    'Content-Length': req.headers['content-length'],
                    'Content-Type': req.headers['content-type']})
            image_resp.nodes = []
            untar_stream = UntarStream(cached_body.cache, path_list)
            try:
                for chunk in untar_stream:
                    req.bytes_transferred += len(chunk)
                    etag.update(chunk)
            except ReadError:
                return HTTPUnprocessableEntity(
                    request=req,
                    body='Error reading %s stream'
                         % req.headers['content-type'])
            for buf in path_list:
                if buf.is_closed:
                    cluster_config = buf.body
                    break
            if not cluster_config:
                return HTTPBadRequest(request=req,
                                      body='System boot map was not '
                                           'found in request')
            try:
                cluster_config = json.loads(cluster_config)
            except Exception:
                return HTTPUnprocessableEntity(body='Could not parse '
                                                    'system map')
        elif req.content_type in 'application/json':
            # System map was sent as a POST body
            if not cluster_config:
                for chunk in read_iter:
                    req.bytes_transferred += len(chunk)
                    if time.time() > upload_expiration:
                        return HTTPRequestTimeout(request=req)
                    if req.bytes_transferred > \
                            self.middleware.zerovm_maxconfig:
                        return HTTPRequestEntityTooLarge(request=req)
                    etag.update(chunk)
                    cluster_config += chunk
		''' cluster_config contains the .json file content.
		ex. [{"file_list": [{"device": "python2.7"}, {"device": "stdout"}, {"device": "image", "path": "swift://AUTH_8b2be51be0c1485480dcb4f6e725cf50/container1/hello.zapp"}], "name": "hello", "exec": {"path": "file://python2.7:python", "args": "hello.py bangladesh"}}]

		'''
		my_debug("cluster_config", cluster_config)
                if 'content-length' in req.headers and \
                   int(req.headers['content-length']) != req.bytes_transferred:
                    return HTTPClientDisconnect(request=req,
                                                body='application/json '
                                                     'POST unfinished')
                etag = etag.hexdigest()
                if 'etag' in req.headers and\
                   req.headers['etag'].lower() != etag:
                    return HTTPUnprocessableEntity(request=req)
            try:
                cluster_config = json.loads(cluster_config)
            except Exception:
                return HTTPUnprocessableEntity(
                    body='Could not parse system map')
        else:
            # assume the posted data is a script and try to execute
            if 'content-length' not in req.headers:
                return HTTPBadRequest(request=req,
                                      body='Must specify Content-Length')
            cached_body = CachedBody(read_iter)
            # all scripts must start with shebang
            if not cached_body.cache[0].startswith('#!'):
                return HTTPBadRequest(request=req,
                                      body='Unsupported Content-Type')
            buf = ''
            shebang = None
            for chunk in cached_body.cache:
                i = chunk.find('\n')
                if i > 0:
                    shebang = buf + chunk[0:i]
                    break
                buf += chunk
            if not shebang:
                return HTTPBadRequest(request=req,
                                      body='Cannot find '
                                           'shebang (#!) in script')
            command_line = re.split('\s+',
                                    re.sub('^#!\s*(.*)', '\\1', shebang), 1)
            sysimage = None
            args = None
            exe_path = command_line[0]
            location = parse_location(exe_path)
            if not location:
                return HTTPBadRequest(request=req,
                                      body='Bad interpreter %s' % exe_path)
            if is_image_path(location):
                if 'image' == location.image:
                    return HTTPBadRequest(request=req,
                                          body='Must supply image name '
                                               'in shebang url %s'
                                               % location.url)
                sysimage = location.image
            if len(command_line) > 1:
                args = command_line[1]
            params = {'exe_path': exe_path}
            if args:
                params['args'] = args.strip() + " "
            if self.container_name and self.object_name:
                template = POST_TEXT_OBJECT_SYSTEM_MAP
                location = SwiftPath.init(self.account_name,
                                          self.container_name,
                                          self.object_name)
                config = _config_from_template(params, template, location.url)
            else:
                template = POST_TEXT_ACCOUNT_SYSTEM_MAP
                config = _config_from_template(params, template, '')

            try:
                cluster_config = json.loads(config)
            except Exception:
                return HTTPUnprocessableEntity(body='Could not parse '
                                                    'system map')
            if sysimage:
                cluster_config[0]['file_list'].append({'device': sysimage})
            string_path = Path(REGTYPE,
                               'script',
                               int(req.headers['content-length']),
                               cached_body)
            stream = TarStream(path_list=[string_path])
            stream_length = stream.get_total_stream_length()
            user_image = True
            image_resp = Response(app_iter=iter(stream),
                                  headers={
                                      'Content-Length': stream_length})
            image_resp.nodes = []


	my_debug("self app",  self.__dict__)
        req.path_info = '/' + self.account_name
        try:
            if self.middleware.ignore_replication:
                replica_count = 1
            else:
                replica_count = self.app.object_ring.replica_count
            self.parser.parse(cluster_config,
                              user_image,
                              self.account_name,
                              replica_count,
                              request=req)
        except ClusterConfigParsingError, e:
            self.app.logger.warn(
                'ERROR Error parsing config: %s', cluster_config)
            return HTTPBadRequest(request=req, body=str(e))

        # for n in self.parser.node_list:
        #     print n.dumps(indent=2)

	my_debug("#self.parser.parse",self.parser.__dict__)

	''' 
	The parser object contains to be created zerovm nodes in 'node_list' variable.
	
	{'default_content_type': 'application/octet-stream', 'list_container': <bound method ProxyQueryMiddleware.list_container of <zerocloud.proxyquery.ProxyQueryMiddleware object at 0x2a8b050>>, 'list_account': <bound method ProxyQueryMiddleware.list_account of <zerocloud.proxyquery.ProxyQueryMiddleware object at 0x2a8b050>>, 'total_count': 1, 'node_list': [<zerocloud.common.ZvmNode object at 0x2b431d0>], 'parser_config': {'limits': {'wbytes': 1073741824, 'rbytes': 1073741824, 'reads': 1073741824, 'writes': 1073741824}}, 'node_id': 2, 'sysimage_devices': {'python2.7': None, '/usr/share/zerovm/python.tar': None}, 'network_type': 'tcp', 'nodes': {'hello': <zerocloud.common.ZvmNode object at 0x2b431d0>}	
	}

	'''

        data_sources = []
        addr = self._get_own_address()
        if not addr:
            return HTTPServiceUnavailable(
                body='Cannot find own address, check zerovm_ns_hostname')
        ns_server = None
        network_is_tcp = self.middleware.network_type == 'tcp'
        if self.parser.total_count > 1 and network_is_tcp:
            ns_server = NameService(self.parser.total_count)
            if self.middleware.zerovm_ns_thrdpool.free() <= 0:
                return HTTPServiceUnavailable(
                    body='Cluster slot not available',
                    request=req)
            ns_server.start(self.middleware.zerovm_ns_thrdpool)
            if not ns_server.port:
                return HTTPServiceUnavailable(body='Cannot bind name service')
        exec_requests = []
	my_debug("#self.parser", self.parser.__dict__)
	my_debug("self.parser.node_list", self.parser.node_list)
        for node in self.parser.node_list:
            nexe_headers = {
                'x-nexe-system': node.name,
                'x-nexe-status': 'ZeroVM did not run',
                'x-nexe-retcode': 0,
                'x-nexe-etag': '',
                'x-nexe-validation': 0,
                'x-nexe-cdr-line': '0.0 0.0 0 0 0 0 0 0 0 0'
            }
            path_info = req.path_info
            exec_request = Request.blank(path_info,
                                         environ=req.environ,
                                         headers=req.headers)
            exec_request.path_info = path_info
            exec_request.etag = None
            exec_request.content_type = TAR_MIMES[0]
            # chunked encoding handling looks broken in Swift
            # but let's leave it here, maybe somebody will fix it
            # exec_request.content_length = None
            # exec_request.headers['transfer-encoding'] = 'chunked'
            exec_request.headers['x-account-name'] = self.account_name
            exec_request.headers['x-timestamp'] = \
                normalize_timestamp(time.time())
            exec_request.headers['x-zerovm-valid'] = 'false'
            exec_request.headers['x-zerovm-pool'] = 'default'
            if len(node.connect) > 0 or len(node.bind) > 0:
                # node operation depends on connection to other nodes
                exec_request.headers['x-zerovm-pool'] = 'cluster'
            if 'swift.authorize' in exec_request.environ:
                aresp = exec_request.environ['swift.authorize'](exec_request)
                if aresp:
                    return aresp
            if ns_server:
                node.name_service = 'udp:%s:%d' % (addr, ns_server.port)
            if self.parser.total_count > 1:
                self.parser.build_connect_string(
                    node, req.headers.get('x-trans-id'))
                if node.replicate > 1:
                    for i in range(0, node.replicate - 1):
                        node.replicas.append(deepcopy(node))
                        node.replicas[i].id = \
                            node.id + (i + 1) * len(self.parser.node_list)
            node.copy_cgi_env(exec_request)
            resp = node.create_sysmap_resp()
            node.add_data_source(data_sources, resp, 'sysmap')
	    my_debug("#replication nodes", node.replicas) # node.replicas=[]
            for repl_node in node.replicas:
                repl_node.copy_cgi_env(exec_request)
                resp = repl_node.create_sysmap_resp()
                repl_node.add_data_source(data_sources, resp, 'sysmap')
            channels = self._get_remote_objects(node)
	    my_debug("#channels with node", channels) # channels = []
            for ch in channels:
                error = self._create_request_for_remote_object(data_sources,
                                                               ch,
                                                               exe_resp,
                                                               req,
                                                               nexe_headers,
                                                               node)
                if error:
                    return error
            if user_image:
                node.last_data = image_resp
                image_resp.nodes.append({'node': node,
                                         'dev': 'image'})
                for repl_node in node.replicas:
                    repl_node.last_data = image_resp
                    image_resp.nodes.append({'node': repl_node,
                                             'dev': 'image'})
            if not getattr(node, 'path_info', None):
                node.path_info = path_info
            exec_request.node = node
            exec_request.resp_headers = nexe_headers
            sock = self.get_daemon_socket(node)
	    my_debug("#local variables are", locals())
	    my_debug("#exec requests are", exec_request.__dict__)
            if sock:
                exec_request.headers['x-zerovm-daemon'] = str(sock)
            exec_requests.append(exec_request)
	
	my_debug("#data source",data_sources)

        if user_image:
            data_sources.append(image_resp)
        tstream = TarStream()
	my_debug("#data sources dict", data_sources[0].__dict__) # start from here.
        for data_src in data_sources:
	    my_debug("#data source in array", data_src.__dict__)
            for n in data_src.nodes:  # data_src.nodes contains list of zerocloud.common.ZvmNode object
                if not getattr(n['node'], 'size', None):
                    n['node'].size = 0
                n['node'].size += len(tstream.create_tarinfo(
                    ftype=REGTYPE,
                    name=n['dev'],
                    size=data_src.content_length))
                n['node'].size += \
                    TarStream.get_archive_size(data_src.content_length)
        pile = GreenPileEx(self.parser.total_count)
	my_debug("pile @1064", pile.__dict__)
	my_debug("exec_requests", exec_requests[0].__dict__)
	'''
		zerovm execution request is made & being sent from here.
	'''
        conns = self._make_exec_requests(pile, exec_requests)
        if len(conns) < self.parser.total_count:
            self.app.logger.exception(
                'ERROR Cannot find suitable node to execute code on')
            for conn in conns:
                close_swift_conn(getattr(conn, 'resp'))
            return HTTPServiceUnavailable(
                body='Cannot find suitable node to execute code on')

        for conn in conns:
            if hasattr(conn, 'error'):
                if hasattr(conn, 'resp'):
                    close_swift_conn(conn.resp)
                return Response(app_iter=[conn.error],
                                status="%d %s" % (conn.resp.status,
                                                  conn.resp.reason),
                                headers=conn.nexe_headers)

        _attach_connections_to_data_sources(conns, data_sources)

        # chunked encoding handling looks broken in Swift
        # but let's leave it here, maybe somebody will fix it
        # chunked = req.headers.get('transfer-encoding')
        chunked = False
        try:
            with ContextPool(self.parser.total_count) as pool:
                self._spawn_file_senders(conns, pool, req)
                for data_src in data_sources:
                    data_src.bytes_transferred = 0
                    _send_tar_headers(chunked, data_src)
                    while True:
                        with ChunkReadTimeout(self.middleware.client_timeout):
                            try:
                                data = next(data_src.app_iter)
                            except StopIteration:
                                error = _finalize_tar_streams(chunked,
                                                              data_src,
                                                              req)
                                if error:
                                    return error
                                break
                        error = _send_data_chunk(chunked, data_src, data, req)
                        if error:
                            return error
                    if data_src.bytes_transferred < data_src.content_length:
                        return HTTPClientDisconnect(
                            request=req,
                            body='data source %s dead' % data_src.__dict__)
                for conn in conns:
                    if conn.queue.unfinished_tasks:
                        conn.queue.join()
                    conn.tar_stream = None
        except ChunkReadTimeout, err:
            self.app.logger.warn(
                'ERROR Client read timeout (%ss)', err.seconds)
            self.app.logger.increment('client_timeouts')
            return HTTPRequestTimeout(request=req)
        except (Exception, Timeout):
            print traceback.format_exc()
            self.app.logger.exception(
                'ERROR Exception causing client disconnect')
            return HTTPClientDisconnect(request=req, body='exception')

        for conn in conns:
            pile.spawn(self._process_response, conn, req)
        do_defer = req.headers.get('x-zerovm-deferred', 'never').lower()
        if do_defer == 'always':
            defer_timeout = 0
        elif do_defer == 'auto':
            defer_timeout = self.middleware.immediate_response_timeout
        else:
            defer_timeout = None
        conns = []
        try:
            with Timeout(seconds=defer_timeout):
                for conn in pile:
                    if conn:
                        conns.append(conn)
        except Timeout:

            def store_deferred_response(deferred_url):
                for conn in pile:
                    if conn:
                        conns.append(conn)
                resp = self.create_final_response(conns, req)
                path = SwiftPath(deferred_url)
                container_info = get_info(self.app, req.environ.copy(),
                                          path.account, path.container,
                                          ret_not_found=True)
                if container_info['status'] == HTTP_NOT_FOUND:
                    # try to create the container
                    cont_req = Request(req.environ.copy())
                    cont_req.path_info = '/%s/%s' % (path.account,
                                                     path.container)
                    cont_req.method = 'PUT'
                    cont_resp = \
                        ContainerController(self.app,
                                            path.account,
                                            path.container).PUT(cont_req)
                    if cont_resp.status_int >= 300:
                        self.app.logger.warn(
                            'Failed to create deferred container: %s'
                            % cont_req.url)
                        return
                resp.input_iter = iter(resp.app_iter)

                def iter_read(chunk_size=None):
                    if chunk_size is None:
                        return ''.join(resp.input_iter)
                    chunk = next(resp.input_iter)
                    return chunk
                resp.read = iter_read

                deferred_put = Request(req.environ.copy())
                deferred_put.path_info = path.path
                deferred_put.method = 'PUT'
                deferred_put.environ['wsgi.input'] = resp
                deferred_put.content_length = resp.content_length
                deferred_resp = ObjectController(self.app,
                                                 path.account,
                                                 path.container,
                                                 path.obj).PUT(deferred_put)
                if deferred_resp.status_int >= 300:
                    self.app.logger.warn(
                        'Failed to create deferred object: %s : %s'
                        % (deferred_put.url, deferred_resp.status))
                report = self._create_deferred_report(resp.headers)
                resp.input_iter = iter([report])
                deferred_put = Request(req.environ.copy())
                deferred_put.path_info = path.path + '.headers'
                deferred_put.method = 'PUT'
                deferred_put.environ['wsgi.input'] = resp
                deferred_put.content_length = len(report)
                deferred_resp = \
                    ObjectController(self.app,
                                     path.account,
                                     path.container,
                                     path.obj + '.headers').PUT(deferred_put)
                if deferred_resp.status_int >= 300:
                    self.app.logger.warn(
                        'Failed to create deferred object: %s : %s'
                        % (deferred_put.url, deferred_resp.status))
            if self.container_name:
                container = self.container_name
            else:
                container = self.middleware.zerovm_registry_path
            if self.object_name:
                obj = self.object_name
            else:
                obj = 'job-%s' % uuid.uuid4()
            deferred_path = SwiftPath.init(self.account_name, container, obj)
            resp = Response(request=req,
                            body=deferred_path.url)
            spawn_n(store_deferred_response, deferred_path.url)
            if ns_server:
                ns_server.stop()
            return resp
        if ns_server:
            ns_server.stop()
        return self.create_final_response(conns, req)

    def process_server_response(self, conn, request, resp):
        conn.resp = resp
        if resp.status_int >= 300:
            conn.error = resp.body
            return conn
        if resp.content_length == 0:
            return conn
        node = conn.cnode
        untar_stream = UntarStream(resp.app_iter)
        bytes_transferred = 0
        while True:
            try:
                data = next(untar_stream.tar_iter)
            except StopIteration:
                break
            untar_stream.update_buffer(data)
            info = untar_stream.get_next_tarinfo()
            while info:
                if 'sysmap' == info.name:
                    untar_stream.to_write = info.size
                    untar_stream.offset_data = info.offset_data
                    _load_channel_data(node, ExtractedFile(untar_stream))
                    info = untar_stream.get_next_tarinfo()
                    continue
                chan = node.get_channel(device=info.name)
                if not chan:
                    conn.error = 'Channel name %s not found' % info.name
                    return conn
                if not chan.path:
                    app_iter = iter(CachedBody(
                        untar_stream.tar_iter,
                        cache=[untar_stream.block[info.offset_data:]],
                        total_size=info.size))
                    resp.app_iter = app_iter
                    resp.content_length = info.size
                    resp.content_type = chan.content_type
                    return conn
                dest_req = Request.blank(chan.path.path,
                                         environ=request.environ,
                                         headers=request.headers)
                dest_req.path_info = chan.path.path
                dest_req.method = 'PUT'
                dest_req.headers['content-length'] = info.size
                untar_stream.to_write = info.size
                untar_stream.offset_data = info.offset_data
                dest_req.environ['wsgi.input'] = ExtractedFile(untar_stream)
                dest_req.content_type = chan.content_type
                error = update_metadata(dest_req, chan.meta)
                if error:
                    conn.error = error
                    return conn
                dest_resp = \
                    ObjectController(self.app,
                                     chan.path.account,
                                     chan.path.container,
                                     chan.path.obj).PUT(dest_req)

		my_debug("channel.path dict", channel.path.__dict__)
                if dest_resp.status_int >= 300:
                    conn.error = 'Status %s when putting %s' \
                                 % (dest_resp.status, chan.path.path)
                    return conn
                info = untar_stream.get_next_tarinfo()
            bytes_transferred += len(data)
        untar_stream = None
        resp.content_length = 0
        return conn

    def _process_response(self, conn, request):
        conn.error = None
        chunk_size = self.middleware.network_chunk_size
        if conn.resp:
            server_response = conn.resp
            resp = Response(status='%d %s' %
                                   (server_response.status,
                                    server_response.reason),
                            app_iter=iter(lambda: server_response.read(
                                chunk_size), ''),
                            headers=dict(server_response.getheaders()))
        else:
            try:
                with Timeout(self.middleware.node_timeout):
                    server_response = conn.getresponse()
                    resp = Response(status='%d %s' %
                                           (server_response.status,
                                            server_response.reason),
                                    app_iter=iter(lambda: server_response.read(
                                        chunk_size), ''),
                                    headers=dict(server_response.getheaders()))
            except (Exception, Timeout):
                self.app.exception_occurred(
                    conn.node, 'Object',
                    'Trying to get final status of POST to %s'
                    % request.path_info)
                resp = HTTPRequestTimeout(
                    body='Timeout: trying to get final status of POST '
                         'to %s' % request.path_info)
        return self.process_server_response(conn, request, resp)

    def _connect_exec_node(self, obj_nodes, part, request,
                           logger_thread_locals, cnode, request_headers):
        self.app.logger.thread_locals = logger_thread_locals
        conn = None
        for node in obj_nodes:
	    my_debug("node.__dict__", node)
	    '''
		{'replication_port': 6013, 'zone': 1, 'weight': 1.0, 'ip': '127.0.0.1', 'region': 1, 'port': 6013, 'replication_ip': '127.0.0.1', 'meta': '', 'device': 'sdb1', 'id': 0}
	    '''
            try:
                with ConnectionTimeout(self.middleware.conn_timeout):
                    request.headers['Connection'] = 'close'
                    request_headers['Expect'] = '100-continue'
                    request_headers['Content-Length'] = str(cnode.size)
                    conn = http_connect(node['ip'], node['port'],
                                        node['device'], part, request.method,
                                        request.path_info, request_headers)
                with Timeout(self.middleware.node_timeout):
                    resp = conn.getexpect()
                conn.node = node
                conn.cnode = cnode
                conn.nexe_headers = request.resp_headers
                if resp.status == HTTP_CONTINUE:
                    conn.resp = None
                    return conn
                elif is_success(resp.status):
                    conn.resp = resp
                    return conn
                elif resp.status == HTTP_INSUFFICIENT_STORAGE:
                    self.app.error_limit(node,
                                         'ERROR Insufficient Storage')
                    conn.error = 'Insufficient Storage'
                    conn.resp = resp
                    resp.nuke_from_orbit()
                elif is_client_error(resp.status):
                    conn.error = resp.read()
                    conn.resp = resp
                    if resp.status == HTTP_NOT_FOUND:
                        conn.error = 'Error %d %s while fetching %s' \
                                     % (resp.status, resp.reason,
                                        request.path_info)
                    else:
                        return conn
                else:
                    self.app.logger.warn('Obj server failed with: %d %s'
                                         % (resp.status, resp.reason))
                    conn.error = resp.read()
                    conn.resp = resp
                    resp.nuke_from_orbit()
            except Exception:
                self.app.exception_occurred(node, 'Object',
                                            'Expect: 100-continue on %s'
                                            % request.path_info)
                if getattr(conn, 'resp'):
                    conn.resp.nuke_from_orbit()
                conn = None
        if conn:
            return conn

    def _store_accounting_data(self, request, connection=None):
        txn_id = request.environ['swift.trans_id']
        acc_object = datetime.datetime.utcnow().strftime('%Y/%m/%d.log')
        if connection:
            body = '%s %s %s (%s) [%s]\n' % (
                datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                txn_id,
                connection.nexe_headers['x-nexe-system'],
                connection.nexe_headers['x-nexe-cdr-line'],
                connection.nexe_headers['x-nexe-status'])
            request.cdr_log.append(body)
            self.app.logger.info('zerovm-cdr %s %s %s (%s) [%s]'
                                 % (self.account_name,
                                    txn_id,
                                    connection.nexe_headers['x-nexe-system'],
                                    connection.nexe_headers['x-nexe-cdr-line'],
                                    connection.nexe_headers['x-nexe-status']))
        else:
            body = ''.join(request.cdr_log)
            append_req = Request.blank('/%s/%s/%s/%s'
                                       % (self.middleware.version,
                                          self.middleware.cdr_account,
                                          self.account_name,
                                          acc_object),
                                       headers={'X-Append-To': '-1',
                                                'Content-Length': len(body),
                                                'Content-Type': 'text/plain'},
                                       body=body)
            append_req.method = 'POST'
            resp = append_req.get_response(self.app)
            if resp.status_int >= 300:
                self.app.logger.warn(
                    'ERROR Cannot write stats for account %s',
                    self.account_name)

    @delay_denial
    @cors_validation
    def GET(self, req):
        if not self.container_name or not self.object_name:
            return HTTPNotFound(request=req, headers=req.headers)
        obj_req = req.copy_get()
        obj_req.method = 'HEAD'
        if obj_req.environ.get('QUERY_STRING'):
            obj_req.environ['QUERY_STRING'] = ''
        run = False
        if self.object_name[-len('.nexe'):] == '.nexe':
            # let's get a small speedup as it's quite possibly an executable
            obj_req.method = 'GET'
            run = True
        controller = ObjectController(
            self.app,
            self.account_name,
            self.container_name,
            self.object_name)
        handler = getattr(controller, obj_req.method, None)
        obj_resp = handler(obj_req)
        if not is_success(obj_resp.status_int):
            return obj_resp
        content = obj_resp.content_type
        if content == 'application/x-nexe':
            run = True
        elif run:
            # speedup did not succeed...
            # still need to read the whole response
            for _junk in obj_resp.app_iter:
                pass
            obj_req.method = 'HEAD'
            run = False
        template = DEFAULT_EXE_SYSTEM_MAP
        error = self._get_content_config(obj_req, content)
        if error:
            return error
        if obj_req.template:
            template = obj_req.template
        elif not run:
            return HTTPNotFound(request=req,
                                body='No application registered for %s'
                                     % content)
        location = SwiftPath.init(self.account_name,
                                  self.container_name,
                                  self.object_name)
        config = _config_from_template(req.params, template, location.url)
        post_req = Request.blank('/%s' % self.account_name,
                                 environ=req.environ,
                                 headers=req.headers)
        post_req.method = 'POST'
        post_req.content_type = 'application/json'
        exe_resp = None
        if obj_req.method in 'GET':
            exe_resp = obj_resp
        return self.POST(post_req, exe_resp=exe_resp, cluster_config=config)

    def _get_content_config(self, req, content_type):
        req.template = None
        cont = self.middleware.zerovm_registry_path
        obj = '%s/config' % content_type
        config_path = '/%s/%s/%s' % (self.account_name, cont, obj)
        memcache_client = cache_from_env(req.environ)
        memcache_key = 'zvmconf' + config_path
        if memcache_client:
            req.template = memcache_client.get(memcache_key)
            if req.template:
                return
        config_req = req.copy_get()
        config_req.path_info = config_path
        config_resp = ObjectController(
            self.app,
            self.account_name,
            cont,
            obj).GET(config_req)
        if config_resp.status_int == 200:
            req.template = ''
            for chunk in config_resp.app_iter:
                req.template += chunk
                if self.middleware.zerovm_maxconfig < len(req.template):
                    req.template = None
                    return HTTPRequestEntityTooLarge(
                        request=config_req,
                        body='Config file at %s is too large' % config_path)
        if memcache_client and req.template:
            memcache_client.set(
                memcache_key,
                req.template,
                time=float(self.middleware.zerovm_cache_config_timeout))

    def _create_deferred_report(self, headers):
        # just dumps headers as a json object for now
        return json.dumps(dict(headers))


def _load_channel_data(node, extracted_file):
    config = json.loads(extracted_file.read())
    for new_ch in config['channels']:
        old_ch = node.get_channel(device=new_ch['device'])
        if old_ch:
            old_ch.content_type = new_ch['content_type']
            if new_ch.get('meta', None):
                for k, v in new_ch.get('meta').iteritems():
                    old_ch.meta[k] = v


def _total_node_count(node_list):
    count = 0
    for n in node_list:
        count += n.replicate
    return count


def _config_from_template(params, template, url):
    for k, v in params.iteritems():
        if k == 'object_path':
            continue
        ptrn = r'\{\.%s(|=[^\}]+)\}'
        ptrn = ptrn % k
        template = re.sub(ptrn, v, template)
    config = template.replace('{.object_path}', url)
    config = re.sub(r'\{\.[^=\}]+=?([^\}]*)\}', '\\1', config)
    return config


def _attach_connections_to_data_sources(conns, data_sources):
    for data_src in data_sources:
        data_src.conns = []
        for node in data_src.nodes:
            for conn in conns:
                if conn.cnode is node['node']:
                    conn.last_data = node['node'].last_data
                    data_src.conns.append({'conn': conn, 'dev': node['dev']})


def _queue_put(conn, data, chunked):
    conn['conn'].queue.put('%x\r\n%s\r\n'
                           % (len(data), data) if chunked else data)


def _send_tar_headers(chunked, data_src):
    for conn in data_src.conns:
        name = conn['dev']
        if name == 'image' and data_src.content_type == 'application/x-gzip':
            name = 'image.gz'
        info = conn['conn'].tar_stream.create_tarinfo(
            ftype=REGTYPE,
            name=name,
            size=data_src.content_length)
        for chunk in conn['conn'].tar_stream.serve_chunk(info):
            if not conn['conn'].failed:
                _queue_put(conn, chunk, chunked)


def _send_data_chunk(chunked, data_src, data, req):
    data_src.bytes_transferred += len(data)
    if data_src.bytes_transferred > MAX_FILE_SIZE:
        return HTTPRequestEntityTooLarge(request=req)
    for conn in data_src.conns:
        for chunk in conn['conn'].tar_stream.serve_chunk(data):
            if not conn['conn'].failed:
                _queue_put(conn, chunk, chunked)
            else:
                return HTTPServiceUnavailable(request=req)


def _finalize_tar_streams(chunked, data_src, req):
    blocks, remainder = divmod(data_src.bytes_transferred, BLOCKSIZE)
    if remainder > 0:
        nulls = NUL * (BLOCKSIZE - remainder)
        for conn in data_src.conns:
            for chunk in conn['conn'].tar_stream.serve_chunk(nulls):
                if not conn['conn'].failed:
                    _queue_put(conn, chunk, chunked)
                else:
                    return HTTPServiceUnavailable(request=req)
    for conn in data_src.conns:
        if conn['conn'].last_data is data_src:
            if conn['conn'].tar_stream.data:
                data = conn['conn'].tar_stream.data
                if not conn['conn'].failed:
                    _queue_put(conn, data, chunked)
                else:
                    return HTTPServiceUnavailable(request=req)
            if chunked:
                conn['conn'].queue.put('0\r\n\r\n')


def _get_local_address(node):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((node['ip'], node['port']))
    result = s.getsockname()[0]
    s.shutdown(socket.SHUT_RDWR)
    s.close()
    return result


def gunzip_iter(data_iter, chunk_size):
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    unc_data = ''
    for chunk in data_iter:
        while dec.unconsumed_tail:
            while len(unc_data) < chunk_size and dec.unconsumed_tail:
                unc_data += dec.decompress(dec.unconsumed_tail,
                                           chunk_size - len(unc_data))
            if len(unc_data) == chunk_size:
                yield unc_data
                unc_data = ''
            if unc_data and dec.unconsumed_tail:
                chunk += dec.unconsumed_tail
                break
        unc_data += dec.decompress(chunk, chunk_size - len(unc_data))
        if len(unc_data) == chunk_size:
            yield unc_data
            unc_data = ''
    if unc_data:
        yield unc_data


def filter_factory(global_conf, **local_conf):
    """
    paste.deploy app factory for creating WSGI proxy apps.
    """
    conf = global_conf.copy()
    conf.update(local_conf)

    def query_filter(app):
        return ProxyQueryMiddleware(app, conf)

    return query_filter
