__author__ = 'Shirish Pal'

import os
import sys
import uuid
import json
import time
import signal
import argparse
import requests
import ipaddress
import importlib
import subprocess
from threading import Thread
from functools import reduce
from pymongo import MongoClient

from .config import NODE_RUNDIR, POD_RUNDIR, NODE_SRCDIR, POD_SRCDIR, TCPDUMP_FLAG
from .config import DB_CSTRING, REGISTRY_DB_NAME, RESULT_DB_NAME, NODE_RUNDIR
from .config import STATS_TABLE, CSTATE_TABLE, LIVE_STATS_TABLE, POD_RUNDIR
from .config import RPC_IP_VETH1, RPC_IP_VETH2, RPC_PORT, NODE_SRCDIR, POD_SRCDIR
from .config import TESTBED_TABLE, RUN_TABLE

def get_pod_name (testbed, pod_index):
    return "{}-pod-{}".format (testbed, pod_index+1)

def get_exe_alias (testbed, pod_index, runid):
    return "{}-{}.exe".format (get_pod_name (testbed, pod_index), runid)

def get_pod_ip (testbed, pod_index):
    pod_name = get_pod_name (testbed, pod_index)
    cmd_str = "docker inspect --format='{{.NetworkSettings.IPAddress}}' " + pod_name
    return subprocess.check_output(cmd_str, shell=True, close_fds=True).decode("utf-8").strip()

def start_run_thread(testbed
                        , pod_index
                        , pod_cfg_file
                        , pod_iface_list
                        , pod_ip
                        , exe_alias):

    url = 'http://{}:{}/start'.format(pod_ip, RPC_PORT)

    data = {'cfg_file': pod_cfg_file
                , 'z_index' : pod_index
                , 'net_ifaces' : pod_iface_list
                , 'rpc_ip_veth1' : RPC_IP_VETH1
                , 'rpc_ip_veth2' : RPC_IP_VETH2
                , 'rpc_port' : RPC_PORT
                , 'exe_alias' : exe_alias}

    resp = requests.post(url, json=data)


def stop_run_thread(testbed
                        , pod_index
                        , pod_iface_list
                        , pod_ip
                        , exe_alias):

    url = 'http://{}:{}/stop'.format(pod_ip, RPC_PORT)

    data = {'net_ifaces' : pod_iface_list
                , 'rpc_ip_veth1' : RPC_IP_VETH1
                , 'rpc_ip_veth2' : RPC_IP_VETH2
                , 'rpc_port' : RPC_PORT
                , 'exe_alias' : exe_alias}

    resp = requests.post(url, data=json.dumps(data))

    # todo


def start_run_stats (runid
                        , server_pod_ips=[]
                        , proxy_pod_ips=[]
                        , client_pod_ips=[]):

    stats_pid_file = os.path.join(NODE_RUNDIR, 'traffic', runid, 'stats_pid.txt')

    pod_ips = ''
    if server_pod_ips:
        pod_ips += ' --server_pod_ips ' + ':'.join(server_pod_ips)
    if proxy_pod_ips:
        pod_ips += ' --proxy_pod_ips ' + ':'.join(proxy_pod_ips)
    if client_pod_ips:
        pod_ips += ' --client_pod_ips ' + ':'.join(client_pod_ips)

    os.system('python3 -m tlspack.Base --runid {} {} & echo $! > {}'. \
                                format (runid, pod_ips, stats_pid_file))

    stats_pid = ''
    with open (stats_pid_file) as f:
        stats_pid = f.read().strip()

    return stats_pid


def stop_run_stats(stats_pid):
    if stats_pid:
        try:
            os.kill (stats_pid, signal.SIGKILL)
        except:
            pass

def is_running (runid):
    mongoClient = MongoClient (DB_CSTRING)
    db = mongoClient[REGISTRY_DB_NAME]
    run_table = db[RUN_TABLE]
    if run_table.find_one ({'runid' : runid}):
        return True
    return False

def run_stats_iter(runid):
    mongoClient = MongoClient (DB_CSTRING)
    db = mongoClient[RESULT_DB_NAME]
    stats_col = db[LIVE_STATS_TABLE]

    while is_running (runid):
        try:
            stats = stats_col.find({'runid' : runid})[0]
        except:
            stats = {}
        yield stats

def next_ipaddr (ip_addr, count):
    return ipaddress.ip_address(ip_addr) + count


class TlsAppError (Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message

        
class TlsAppRun:
    def __init__(self, runid, new_run=True):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        run_table = db[RUN_TABLE]
        if new_run:
            if run_table.find_one ({'runid' : runid}):
                raise TlsAppError(-1,  'error: {} already runing'.format (runid))
        else:
            if not run_table.find_one ({'runid' : runid}):
                raise TlsAppError(-1,  'error: invlaid runid'.format (runid))

        self.runid = runid

    @property
    def testbed (self):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        run_table = db[RUN_TABLE]
        return run_table.find_one ({'runid' : self.runid}).get('testbed', '')

    @testbed.setter
    def testbed (self, value):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        run_table = db[RUN_TABLE]
        run_table.insert ({'runid' : self.runid, 'testbed' : value})

    @property
    def stats_pid(self):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        run_table = db[RUN_TABLE]
        return run_table.find_one ({'runid' : self.runid}).get('stats_pid', '')

    @stats_pid.setter
    def stats_pid(self, value):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        run_table = db[RUN_TABLE]
        run_table.update ({'runid' : self.runid}
                            , {"$set": { "stats_pid": value }})

    def dispose(self):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        run_table = db[RUN_TABLE]
        run_table.remove ({'runid' : self.runid})
        self.runid = ''


class TlsAppTestbed:
    def __init__(self, testbed):
        self.testbed = testbed
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        testbed_table = db[TESTBED_TABLE]
        if not testbed_table.find_one({'testbed' : self.testbed}):
            raise TlsAppError(-1, 'testbed {} does not exist'.format (self.testbed))

    @property
    def type(self):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        testbed_table = db[TESTBED_TABLE]
        return testbed_table.find_one({'testbed' : self.testbed}).get('type', '')       

    @property
    def ready(self):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        testbed_table = db[TESTBED_TABLE]
        return testbed_table.find_one({'testbed' : self.testbed}).get('ready', 0)

    @ready.setter
    def ready(self, value):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        testbed_table = db[TESTBED_TABLE]
        testbed_table.update({'testbed' : self.testbed}, {"$set": { 'ready': value}})
    
    @property
    def runid(self):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        testbed_table = db[TESTBED_TABLE]
        return testbed_table.find_one({'testbed' : self.testbed}).get('runing', '')

    @runid.setter
    def runid(self, value):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        testbed_table = db[TESTBED_TABLE]
        testbed_table.update({'testbed' : self.testbed}, {"$set": { 'runing': value }})

    @property
    def busy(self):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        testbed_table = db[TESTBED_TABLE]
        if testbed_table.find_one({'testbed' : self.testbed}).get('runing'):
            return True
        return False

    def get_info(self):
        mongoClient = MongoClient (DB_CSTRING)
        db = mongoClient[REGISTRY_DB_NAME]
        testbed_table = db[TESTBED_TABLE]
        return testbed_table.find_one({'testbed' : self.testbed})


class TlsCsAppTestbed (TlsAppTestbed):
    def __init__(self, testbed):

        super().__init__(testbed)

        testbed_info = self.get_info()

        self.pod_iface = 'eth1'
        self.traffic_paths = testbed_info['traffic_paths']
        self.traffic_path_count = len(self.traffic_paths)
        
    def start_pod(self, pod_index, testbed_info, traffic_path, client):
        rundir_map = "--volume={}:{}".format (NODE_RUNDIR, POD_RUNDIR)
        srcdir_map = "--volume={}:{}".format (NODE_SRCDIR, POD_SRCDIR)

        pod_name = get_pod_name (self.testbed, pod_index)
        if client:
            node_iface = traffic_path['client']['iface']
        else:
            node_iface = traffic_path['server']['iface']

        cmd_str = "sudo docker run --cap-add=SYS_PTRACE --security-opt seccomp=unconfined --network=bridge --privileged --name {} -it -d {} {} tlspack/tgen:latest /bin/bash".format (pod_name, rundir_map, srcdir_map)
        os.system (cmd_str)

        pod_ip = get_pod_ip (self.testbed, pod_index)

        cmd_str = "sudo ip link set dev {} up".format(node_iface)
        os.system (cmd_str)

        node_macvlan = testbed_info[node_iface]['macvlan']
        cmd_str = "sudo docker network connect {} {}".format(node_macvlan, pod_name)
        os.system (cmd_str)

        cmd_str = "sudo docker exec -d {} echo '/rundir/cores/core.%t.%e.%p' | tee /proc/sys/kernel/core_pattern".format(pod_name)
        os.system (cmd_str)

        cmd_str = "sudo docker exec -d {} cp -f /rundir/bin/tlspack.exe /usr/local/bin".format(pod_name)
        os.system (cmd_str)

        cmd_str = "sudo docker exec -d {} chmod +x /usr/local/bin/tlspack.exe".format(pod_name)
        os.system (cmd_str)

        cmd_str = "sudo docker exec -d {} cp -f /rundir/bin/rpc_proxy_main.py /usr/local/bin".format(pod_name)
        os.system (cmd_str)

        cmd_str = "sudo docker exec -d {} chmod +x /usr/local/bin/rpc_proxy_main.py".format(pod_name)
        os.system (cmd_str)

        cmd_str = "sudo docker exec -d {} python3 /usr/local/bin/rpc_proxy_main.py {} {}".format(pod_name, pod_ip, RPC_PORT)
        os.system (cmd_str)


    def start(self):

        testbed_info = self.get_info()

        pod_index = -1
        for traffic_path in testbed_info['traffic_paths']:
            #client
            pod_index += 1
            self.start_pod(pod_index, testbed_info, traffic_path, client=True)

            #server
            pod_index += 1
            self.start_pod(pod_index, testbed_info, traffic_path, client=False)

        self.ready = 1


    def stop(self):
        pass


class TlsApp(object):
    def __init__(self):
        self.pod_rundir_certs = os.path.join(POD_RUNDIR, 'certs')

        self.tcpdump = TCPDUMP_FLAG
        self.next_ipaddr = next_ipaddr
        self.stats_iter = None
        
        self.run_uid = str(uuid.uuid4())
        self.next_log_index = 0

        self.runI = None
        self.testbedI = None
        self.app_testbed_type = None

    def __del__(self):
        pass

    def log (self, msg):
        pass
        self.next_log_index += 1
        return msg

    @staticmethod
    def stop_run(runid):
        _runI = TlsAppRun (runid, new_run=False)

        testbed_class_name = TlsAppTestbed(_runI.testbed).type + 'Testbed'
        testbed_class = getattr(sys.modules[__name__], testbed_class_name)
        _testbedI = testbed_class (_runI.testbed)

        app_class_name = TlsAppTestbed(_runI.testbed).type
        app_class = getattr(sys.modules[__name__], app_class_name)

        app_class.stop_run(_runI, _testbedI)


    def start_init(self, testbed, runid):

        # runid info
        _runI = TlsAppRun(runid)

        self.pod_pcap_dir = os.path.join(POD_RUNDIR
                                    , 'traffic'
                                    , runid
                                    , 'pcaps')

        # testbed info
        testbed_class_name = self.app_testbed_type + 'Testbed'
        testbed_class = getattr(sys.modules[__name__], testbed_class_name)
        _testbedI = testbed_class (testbed)

        # testbed compatibility
        if not _testbedI.type == self.app_testbed_type:
            raise TlsAppError(-1
            , 'error: incompatible testbed type {}'.format (_testbedI.type))

        # testbed availability
        if _testbedI.busy:
            raise TlsAppError(-1
            , 'error: testbed {} in use; running {}'.format \
            (_testbedI.testbed, _testbedI.runid))

        # testbed readiness
        if not _testbedI.ready:
            _testbedI.start()

        self.runI = _runI
        self.testbedI = _testbedI

    def start_run (self, config_j):
        node_cfg_dir = os.path.join(NODE_RUNDIR, 'traffic', self.runI.runid)

        os.system ( 'rm -rf {}'.format(node_cfg_dir) )
        os.system ( 'mkdir -p {}'.format(node_cfg_dir) )
        os.system ( 'mkdir -p {}'.format(os.path.join(node_cfg_dir, 'pcaps')) )
        os.system ( 'mkdir -p {}'.format(os.path.join(node_cfg_dir, 'logs')) )

        node_cfg_file = os.path.join(node_cfg_dir, 'config.json')
        config_s = json.dumps(config_j, indent=2)
        with open(node_cfg_file, 'w') as f:
            f.write(config_s)

    def stop (self):
        self.runI = None
        self.testbedI = None
        
class TlsCsApp(TlsApp):
    def __init__ (self):

        super().__init__()

        self.app_testbed_type = 'TlsCsApp'

        self.max_active = 1
        self.max_pipeline = 1
        self.tcp_snd_buff = 0
        self.tcp_rcv_buff = 0
        self.app_snd_buff = 0
        self.app_rcv_buff = 0
        self.app_next_write = 0
        self.app_cs_starttls_len = 0
        self.app_sc_starttls_len = 0
        self.app_cs_data_len = 1
        self.app_sc_data_len = 1
        self.total_conn_count = 1
        self.close_type = 'fin'
        self.close_notify = 'no_send'
        self.session_resumption = 0
        self.emulation_id = 0
        self.client_port_begin = 5000
        self.client_port_end = 65000

    def start_init (self, testbed, runid):
        super().start_init (testbed, runid)

    def start_run (self, config_j):

        super().start_run (config_j)

        pod_cfg_file = os.path.join(POD_RUNDIR
                                    , 'traffic'
                                    , self.runI.runid
                                    , 'config.json')

        testbed_info = self.testbedI.get_info()

        # start the server
        server_pod_ips = []
        pod_start_threads = []
        pod_index = 1
        for traffic_path in testbed_info['traffic_paths']:

            pod_ip = get_pod_ip (self.testbedI.testbed, pod_index)
            server_pod_ips.append (pod_ip)

            exe_alias = get_exe_alias (self.testbedI.testbed
                                        , pod_index
                                        , self.runI.runid)

            thd = Thread(target=start_run_thread
                        , args=[self.testbedI.testbed
                                , pod_index
                                , pod_cfg_file
                                , [self.testbedI.pod_iface]
                                , pod_ip
                                , exe_alias])
            thd.daemon = True
            thd.start()
            pod_start_threads.append(thd)

            pod_index += 2

        if pod_start_threads:
            for thd in pod_start_threads:
                thd.join()
            time.sleep(1)


        # start the clients
        client_pod_ips = []
        pod_start_threads = []
        pod_index = 0
        for traffic_path in testbed_info['traffic_paths']:

            pod_ip = get_pod_ip (self.testbedI.testbed, pod_index)
            client_pod_ips.append (pod_ip)

            exe_alias = get_exe_alias (self.testbedI.testbed
                                        , pod_index
                                        , self.runI.runid)

            thd = Thread(target=start_run_thread
                        , args=[self.testbedI.testbed
                                , pod_index
                                , pod_cfg_file
                                , [self.testbedI.pod_iface]
                                , pod_ip
                                , exe_alias])
            thd.daemon = True
            thd.start()
            pod_start_threads.append(thd)

            pod_index += 2

        if pod_start_threads:
            for thd in pod_start_threads:
                thd.join()
            time.sleep(1)

        self.testbedI.runid = self.runI.runid
        self.runI.testbed = self.testbedI.testbed

        self.runI.stats_pid = start_run_stats (self.runI.runid
                                , server_pod_ips = server_pod_ips
                                , client_pod_ips = client_pod_ips)

    @staticmethod
    def stop_run (runI, testbedI):

        testbed_info = testbedI.get_info()

        # stop the clients
        pod_stop_threads = []
        pod_index = 0
        for traffic_path in testbed_info['traffic_paths']:

            pod_ip = get_pod_ip (testbedI.testbed, pod_index)

            exe_alias = get_exe_alias (testbedI.testbed
                                        , pod_index
                                        , runI.runid)

            thd = Thread(target=stop_run_thread
                        , args=[testbedI.testbed
                                , pod_index
                                , [testbedI.pod_iface]
                                , pod_ip
                                , exe_alias])
            thd.daemon = True
            thd.start()
            pod_stop_threads.append(thd)
            pod_index += 2

        if pod_stop_threads:
            for thd in pod_stop_threads:
                thd.join()
            time.sleep(1)


        # stop the servers
        pod_stop_threads = []
        pod_index = 1
        for traffic_path in testbed_info['traffic_paths']:

            pod_ip = get_pod_ip (testbedI.testbed, pod_index)

            exe_alias = get_exe_alias (testbedI.testbed
                                        , pod_index
                                        , runI.runid)

            thd = Thread(target=stop_run_thread
                        , args=[testbedI.testbed
                                , pod_index
                                , [testbedI.pod_iface]
                                , pod_ip
                                , exe_alias])
            thd.daemon = True
            thd.start()
            pod_stop_threads.append(thd)

            pod_index += 2

        if pod_stop_threads:
            for thd in pod_stop_threads:
                thd.join()
            time.sleep(1)

        stop_run_stats (runI.stats_pid)
        testbedI.runid = ''
        runI.dispose ()


    def stop(self):

        if not self.runI or not self.runI.testbed:
            raise TlsAppError(-1, 'error: not running')

        TlsCsApp.stop_run (self.runI, self.testbedI)

        super().stop()


    def stats (self):
        if not self.stats_iter:
            self.stats_iter = run_stats_iter (self.runI.runid)
        return next (self.stats_iter, None)


def get_stats (pod_ips):
    stats_list = []
    stats_keys = []
    for pod_ip in pod_ips:
        try:
            resp = requests.get('http://{}:{}/ev_sockstats'.format(pod_ip, RPC_PORT))
            stats_j = resp.json()
            stats_list.append (stats_j)
            if stats_j:
                stats_keys = stats_j.keys()
        except:
            stats_list.append ({})

    stats_sum = {}
    for stats_key in stats_keys:
        stats_values = map(lambda s : s.get(stats_key, 0), stats_list) 
        stats_sum[stats_key] = reduce(lambda x, y : x + y, stats_values)

    return (stats_sum, stats_list)

def collect_stats (runid
                    , server_pod_ips
                    , proxy_pod_ips
                    , client_pod_ips):
    max_tick = 1

    mongoClient = MongoClient (DB_CSTRING)
    db = mongoClient[RESULT_DB_NAME]
    stats_col = db[LIVE_STATS_TABLE]

    stats_col.remove({'runid' : runid})

    tick = 0
    while True:
        tick += 1

        server_stats, server_stats_list  = get_stats (server_pod_ips)
        proxy_stats, proxy_stats_list = get_stats (proxy_pod_ips)
        client_stats, client_stats_list = get_stats (client_pod_ips)
        
        stats_col.insert({'tick' : tick
                                , 'runid' : runid

                                , 'server_stats' : server_stats
                                , 'proxy_stats' : proxy_stats
                                , 'client_stats' : client_stats
                                , 'server_stats_list' : server_stats_list
                                , 'proxy_stats_list' : proxy_stats_list
                                , 'client_stats_list' : client_stats_list
                                
                                })
        if tick > max_tick:
            stats_col.remove({'tick' : tick-max_tick})

        time.sleep(0.1)


def get_arguments ():

    arg_parser = argparse.ArgumentParser(description = 'stats')

    arg_parser.add_argument('--runid'
                                , action="store"
                                , required=True
                                , help = 'run id')
                                
    arg_parser.add_argument('--server_pod_ips'
                                , action="store"
                                , default=''
                                , help = 'run id')

    arg_parser.add_argument('--proxy_pod_ips'
                                , action="store"
                                , default=''
                                , help = 'run id')

    arg_parser.add_argument('--client_pod_ips'
                                , action="store"
                                , default=''
                                , help = 'run id')

    return arg_parser.parse_args()


if __name__ == '__main__':
    c_args = get_arguments ()

    server_pod_ips = c_args.server_pod_ips.split(':')
    proxy_pod_ips = c_args.proxy_pod_ips.split(':')
    client_pod_ips = c_args.client_pod_ips.split(':')

    collect_stats (c_args.runid
                    , server_pod_ips
                    , proxy_pod_ips
                    , client_pod_ips)



        
        
      
