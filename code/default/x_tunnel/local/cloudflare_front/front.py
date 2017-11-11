import time
import os
import threading
import collections
from xlog import getLogger
xlog = getLogger("cloudflare_front")
xlog.set_buffer(500)
import simple_http_client
from config import config

import http_dispatcher
import connect_control
import check_ip


class Front(object):
    name = "cloudflare_front"

    def __init__(self):
        self.dispatchs = {}
        threading.Thread(target=self.update_front_domains).start()
        self.last_success_time = time.time()
        self.last_fail_time = 0
        self.continue_fail_num = 0
        self.success_num = 0
        self.fail_num = 0
        self.last_host = "center.xx-net.net"

        self.rtts = collections.deque([(0, time.time())])
        self.rtts_lock = threading.Lock()
        self.traffics = collections.deque()
        self.traffics_lock = threading.Lock()
        self.recent_sent = 0
        self.recent_received = 0
        self.total_sent = 0
        self.total_received = 0

        threading.Thread(target=self.debug_data_clearup_thread).start()

    @staticmethod
    def update_front_domains():
        next_update_time = time.time()
        while connect_control.keep_running:
            if time.time() < next_update_time:
                time.sleep(4)
                continue

            try:
                client = simple_http_client.HTTP_client("raw.githubusercontent.com", use_https=True)
                path = "/XX-net/XX-Net/master/code/default/x_tunnel/local/cloudflare_front/front_domains.json"
                content, status, response = client.request("GET", path)
                if status != 200:
                    xlog.warn("update front domains fail:%d", status)
                    raise Exception("status:%r", status)

                front_domains_fn = os.path.join(config.DATA_PATH, "front_domains.json")
                if os.path.exists(front_domains_fn):
                    with open(front_domains_fn, "r") as fd:
                        old_content = fd.read()
                        if content != old_content:
                            with open(front_domains_fn, "w") as fd:
                                fd.write(content)
                            check_ip.update_front_domains()

                next_update_time = time.time() + (4 * 3600)
                xlog.info("updated cloudflare front domains from github.")
            except Exception as e:
                next_update_time = time.time() + (1800)
                xlog.debug("updated cloudflare front domains from github fail:%r", e)

    def log_debug_data(self, rtt, sent, received):
        now = time.time()

        self.rtts.append((rtt, now))

        with self.traffics_lock:
            self.traffics.append((sent, received, now))
            self.recent_sent += sent
            self.recent_received += received
            self.total_sent += sent
            self.total_received += received

    def get_rtt(self):
        now = time.time()

        while len(self.rtts) > 1:
            with self.rtts_lock:
                rtt, log_time = rtt_log = max(self.rtts)

                if now - log_time > 5:
                    self.rtts.remove(rtt_log)
                    continue

            return rtt

        return self.rtts[0][0]

    def debug_data_clearup_thread(self):
        while True:
            now = time.time()

            with self.rtts_lock:
                if len(self.rtts) > 1 and now - self.rtts[0][-1] > 5:
                    self.rtts.popleft()

            with self.traffics_lock:
                if self.traffics and now - self.traffics[0][-1] > 60:
                    sent, received, _ = self.traffics.popleft()
                    self.recent_sent -= sent
                    self.recent_received -= received

            time.sleep(0.01)

    def worker_num(self):
        host = self.last_host
        if host not in self.dispatchs:
            self.dispatchs[host] = http_dispatcher.HttpsDispatcher(host, self.log_debug_data)

        dispatcher = self.dispatchs[host]
        return len(dispatcher.workers)

    def get_score(self, host=None):
        now = time.time()
        if now - self.last_fail_time < 5*60 and \
                self.continue_fail_num > 10:
            return None

        if host is None:
            host = self.last_host

        if host not in self.dispatchs:
            self.dispatchs[host] = http_dispatcher.HttpsDispatcher(host, self.log_debug_data)

        dispatcher = self.dispatchs[host]
        worker = dispatcher.get_worker(nowait=True)
        if not worker:
            return None

        return worker.get_score()

    def request(self, method, host, path="/", headers={}, data="", timeout=120):
        if host not in self.dispatchs:
            self.dispatchs[host] = http_dispatcher.HttpsDispatcher(host, self.log_debug_data)

        self.last_host = host

        dispatcher = self.dispatchs[host]
        response = dispatcher.request(method, host, path, headers, data, timeout=timeout)
        status = response.status
        if status not in [200, 405]:
            # xlog.warn("front request %s %s%s fail, status:%d", method, host, path, status)
            self.fail_num += 1
            self.continue_fail_num += 1
            self.last_fail_time = time.time()
        else:
            self.success_num += 1
            self.continue_fail_num = 0

        content = response.task.read_all()
        if status == 200:
            xlog.debug("%s %s%s status:%d trace:%s", method, response.worker.ssl_sock.host, path, status,
                       response.task.get_trace())
        else:
            xlog.warn("%s %s%s status:%d trace:%s", method, response.worker.ssl_sock.host, path, status,
                       response.task.get_trace())
        return content, status, response

    def stop(self):
        connect_control.keep_running = False


front = Front()
