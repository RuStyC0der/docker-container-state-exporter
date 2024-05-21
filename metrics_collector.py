#!/usr/bin/env python3
import requests
import socket

from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from requests.adapters import HTTPAdapter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

class DockerConnection(HTTPConnection):

    docker_socket_path = "/var/run/docker.sock"

    def __init__(self):
        super().__init__("localhost")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.docker_socket_path)


class DockerConnectionPool(HTTPConnectionPool):
    def __init__(self):
        super().__init__("localhost")

    def _new_conn(self):
        return DockerConnection()


class DockerAdapter(HTTPAdapter):
    def get_connection(self, url, proxies=None):
        return DockerConnectionPool()

class MetricsHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/metrics":
            try:
                metrics = self.server.metrics_getter()
                
                self.send_response(200)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(metrics.encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write("Something went wrong.".encode('utf-8'))
                raise e
        else:
            self.send_response(404)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write("Not found".encode('utf-8'))

class MetricsHTTPServer(HTTPServer):

    def set_metrics_getter(self, metrics_getter):
        self.metrics_getter = metrics_getter


# todo fix python 3.10 and older compatibility (write format string)
def iso_time_string_to_seconds_timestamp(str_to_parse):

    zero_time_string = "0001-01-01T00:00:00Z"

    if str_to_parse == zero_time_string: 
        return 0

    format_string = "%Y-%m-%dT%H:%M:%S.%f"

    # strip Z and 3 last digin to convert nanosecond into microseconds as soon as strptime does not support nanosecond
    str_to_parse = str_to_parse[:26]

    timestamp = int(datetime.strptime(str_to_parse, format_string).timestamp())


    if timestamp < 0:
        return 0

    return timestamp

def get_metrics_map_from_raw_info(info_map_list) -> map:
    
    metric_name_prefix = "container_status"

    metrics_map = {
        f"{metric_name_prefix}_state": {
            "type": "gauge",
            "help": "Current state of the container (e.g., running, exited).",
            "values": []
        },
        f"{metric_name_prefix}_exit_code": {
            "type": "gauge",
            "help": "Exit code of the container after it stops.",
            "values": []
        },
        f"{metric_name_prefix}_restart_count": {
            "type": "counter",
            "help": "Number of times the container has been restarted.",
            "values": []
        },        
        f"{metric_name_prefix}_started_seconds": {
            "type": "gauge",
            "help": "Timestamp indicating when the container was started (in seconds since Unix epoch).",
            "values": []
        },
        f"{metric_name_prefix}_finished_seconds": {
            "type": "gauge",
            "help": "Timestamp indicating when the container finished (in seconds since Unix epoch).",
            "values": []
        },
        f"{metric_name_prefix}_created_seconds": {
            "type": "gauge",
            "help": "Timestamp indicating when the container was created (in seconds since Unix epoch).",
            "values": []
        },
    }

    for info_map in info_map_list:

        attributes_map = {
            "id": info_map["Id"],
            "name": info_map["Name"][1:],
        }

        metrics_map[f"{metric_name_prefix}_state"]["values"].extend([
            {
                "value": int(info_map["State"]["OOMKilled"]),
                "attributes": {"status": "oom_killed", **attributes_map}
            },
            {
                "value": int(info_map["State"]["Running"]),
                "attributes": {"status": "running", **attributes_map}
            },
            {
                "value": int(info_map["State"]["Paused"]),
                "attributes": {"status": "paused", **attributes_map}
            },
            {
                "value": int(info_map["State"]["Restarting"]),
                "attributes": {"status": "restarting", **attributes_map}
            },
            {
                "value": int(info_map["State"]["Dead"]),
                "attributes": {"status": "dead", **attributes_map}
            },
        ])
        metrics_map[f"{metric_name_prefix}_exit_code"]["values"].append(
            {
                    "value": int(info_map["State"]["ExitCode"]),
                    "attributes": attributes_map
            }
        )
        metrics_map[f"{metric_name_prefix}_restart_count"]["values"].append(
            {
                    "value": int(info_map["RestartCount"]),
                    "attributes": attributes_map
            }
        )
        metrics_map[f"{metric_name_prefix}_started_seconds"]["values"].append(
            {
                    "value": iso_time_string_to_seconds_timestamp(info_map["State"]["StartedAt"]),
                    "attributes": attributes_map
            }
        )
        metrics_map[f"{metric_name_prefix}_finished_seconds"]["values"].append(
            {
                    "value": iso_time_string_to_seconds_timestamp(info_map["State"]["FinishedAt"]),
                    "attributes": attributes_map
            }
        )
        metrics_map[f"{metric_name_prefix}_created_seconds"]["values"].append(
            {
                    "value": iso_time_string_to_seconds_timestamp(info_map["Created"]),
                    "attributes": attributes_map
            }
        )

    return metrics_map

def compile_prometheus_metrics_string(metrics_map):

    complete_metric_string_list = []
    for metric_name, metric in metrics_map.items():
        help_string = f'# HELP {metric_name} {metric["help"]}'
        type_string = f'# TYPE {metric_name} {metric["type"]}'
        metric_string_list = []
        for value in metric["values"]:
            metric_attributes_string = ",".join(map(lambda key: f'{key}="{value["attributes"][key]}"', value["attributes"]))
            if metric_attributes_string:
                metric_string = f'{metric_name}{{{metric_attributes_string}}} {value["value"]}'
            else:
                metric_string = f'{metric_name} {value["value"]}'

            metric_string_list.append(metric_string)
                
        
        complete_metric_string = help_string + "\n" + type_string + "\n" + "\n".join(metric_string_list)
        complete_metric_string_list.append(complete_metric_string)
    return "\n".join(complete_metric_string_list)

def get_metrics():
    session = requests.Session()
    session.mount("http://docker/", DockerAdapter())
    container_list_raw = session.get("http://docker/containers/json?all=true")
    container_ids_list = [x["Id"] for x in container_list_raw.json()]
    container_raw_metrics_list = [session.get(f"http://docker/containers/{container_id}/json").json() for container_id in container_ids_list]

    metrics_map = get_metrics_map_from_raw_info(container_raw_metrics_list)
    container_metrics = compile_prometheus_metrics_string(metrics_map)

    return container_metrics

def run_server(server_class, handler_class, metrics_getter_function, port=8080):
    server_address = ('', port)
    httpd = server_class(server_address, handler_class)
    httpd.set_metrics_getter(metrics_getter_function)
    try:
        httpd.serve_forever()
    except Exception as e:
        httpd.server_close()
        raise e

if __name__ == "__main__":
    run_server(server_class=MetricsHTTPServer,
        handler_class=MetricsHandler,
        metrics_getter_function=get_metrics,
        port=8080)
