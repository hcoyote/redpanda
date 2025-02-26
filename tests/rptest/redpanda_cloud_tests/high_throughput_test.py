# Copyright 2023 Redpanda Data, Inc.
#
# Use of this software is governed by the Business Source License
# included in the file licenses/BSL.md
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0

import random
import itertools
import math
import re
import time
import json

from threading import Thread

from ducktape.errors import TimeoutError as TimeoutException
from ducktape.mark import ignore, ok_to_fail, parametrize
from ducktape.tests.test import TestContext
from ducktape.utils.util import wait_until
from rptest.clients.installpack import InstallPackClient
from rptest.clients.rpk import RpkTool
from rptest.clients.types import TopicSpec
from rptest.services.cluster import cluster
from contextlib import contextmanager
from rptest.services.failure_injector import FailureInjector, FailureSpec
from rptest.services.kgo_verifier_services import (
    KgoVerifierConsumerGroupConsumer, KgoVerifierProducer,
    KgoVerifierRandomConsumer)
from rptest.services.metrics_check import MetricCheck
from rptest.services.openmessaging_benchmark import OpenMessagingBenchmark
from rptest.services.openmessaging_benchmark_configs import \
    OMBSampleConfigurations
from rptest.services.producer_swarm import ProducerSwarm
from rptest.services.redpanda_cloud import CloudTierName, get_config_profile_name
from rptest.services.redpanda import (RESTART_LOG_ALLOW_LIST, MetricsEndpoint,
                                      SISettings, RedpandaServiceCloud)
from rptest.services.rpk_consumer import RpkConsumer
#from rptest.tests.redpanda_test import RedpandaTest
from rptest.tests.prealloc_nodes import PreallocNodesTest
from rptest.util import firewall_blocked
from rptest.utils.node_operations import NodeDecommissionWaiter
from rptest.utils.si_utils import nodes_report_cloud_segments
from rptest.redpanda_cloud_tests.cloudv2_object_store_blocked import cloudv2_object_store_blocked

KiB = 1024
MiB = KiB * KiB
GiB = KiB * MiB
minutes = 60
hours = 60 * minutes

MachineTypeMemory = {
    'i3en.large': 16 * GiB,
    'i3en.xlarge': 32 * GiB,
    'i3en.2xlarge': 64 * GiB,
    'i3en.3xlarge': 96 * GiB,
    'i3en.6xlarge': 192 * GiB,
    'im4gn.large': 8 * GiB,
    'im4gn.xlarge': 16 * GiB,
    'im4gn.8xlarge': 128 * GiB,
    'n2d-standard-2': 8 * GiB,
    'n2d-standard-4': 16 * GiB,
    'n2d-standard-16': 64 * GiB,
    'n2-standard-2': 8 * GiB,
    'n2-standard-4': 16 * GiB,
    'n2-standard-8': 32 * GiB,
}


class HighThroughputTestTrafficGenerator:
    """
    Generic traffic generator that skews the consumer load to be a multiple
    of the produce load. This is because the predefined cloud v2 tiers have
    higher egress maximum allowed load then ingress, by a factor of at most
    3x in some tiers.
    """
    def __init__(self, context, redpanda, topic, msg_size, asymetry=3):
        t = time.time()
        self._logger = redpanda.logger
        self._producer_start_time = t
        self._producer_end_time = t
        self._consumer_start_time = t
        self._consumer_end_time = t
        self._msg_size = msg_size
        self._msg_count = 5_000_000_000_000
        self._producer = KgoVerifierProducer(context,
                                             redpanda,
                                             topic,
                                             msg_size=self._msg_size,
                                             msg_count=self._msg_count)

        # Level of asymetry is equal to the number of parallel consumers
        self._consumer = KgoVerifierConsumerGroupConsumer(
            context,
            redpanda,
            topic,
            self._msg_size,
            asymetry,
            loop=True,
            max_msgs=self._msg_count,
            debug_logs=True,
            trace_logs=True)

    def wait_for_traffic(self, acked=1, timeout_sec=60):
        wait_until(lambda: self._producer.produce_status.acked >= acked,
                   timeout_sec=timeout_sec,
                   backoff_sec=1.0)

    def start(self):
        self._logger.info("Starting producer")
        self._producer.start()
        self._producer_start_time = time.time()
        # Bump timeout to 120 with ARM and lowerTCO in mind
        self.wait_for_traffic(acked=1, timeout_sec=120)
        # Give the producer a head start
        time.sleep(3)  # TODO: Edit maybe make this configurable?
        self._logger.info("Starting consumer")
        self._consumer.start()
        self._consumer_start_time = time.time()
        wait_until(
            lambda: self._consumer.consumer_status.validator.total_reads >= 1,
            timeout_sec=120)

    def stop(self):
        self._logger.info("Stopping all traffic generation")
        self._producer.stop()
        self._consumer.stop()
        self._producer.wait()
        self._logger.info("Producer stopped")
        self._producer_stop_time = time.time()
        self._consumer.wait()
        self._logger.info("Consumer stopped")
        self._consumer_stop_time = time.time()

    def throughput(self):
        producer_total_time = self._producer_stop_time - self._producer_start_time
        consumer_total_time = self._consumer_stop_time - self._consumer_start_time
        if producer_total_time == 0 or consumer_total_time == 0:
            return {}
        producer_status = self._producer.produce_status
        consumer_status = self._consumer.consumer_status
        producer_bytes_written = self._msg_size * producer_status.acked
        consumer_bytes_read = self._msg_size * consumer_status.validator.total_reads
        return {
            'producer': {
                'errors': producer_status.bad_offsets,
                'total_successful_requests': producer_status.acked,
                'bytes_written': producer_bytes_written,
                'ingress_throughput':
                producer_bytes_written / producer_total_time
            },
            'consumer': {
                'errors': consumer_status.errors,
                'total_successful_requests':
                consumer_status.validator.total_reads,
                'bytes_read': consumer_bytes_read,
                'egress_throughput': consumer_bytes_read / consumer_total_time
            }
        }


@contextmanager
def traffic_generator(context, redpanda, expected_ingress_rate,
                      expected_egress_rate, *args, **kwargs):
    tgen = HighThroughputTestTrafficGenerator(context, redpanda, *args,
                                              **kwargs)
    tgen.start()
    try:
        yield tgen
    except:
        redpanda.logger.exception("Exception within traffic_generator method")
        raise
    finally:
        tgen.stop()
        throughput = tgen.throughput()
        redpanda.logger.info(
            f'HighThroughputTrafficGenerator reported throughput: {json.dumps(throughput, sort_keys=True, indent=4)}'
        )
        producer_throughput = throughput['producer']['ingress_throughput']
        consumer_throughput = throughput['consumer']['egress_throughput']
        assert (
            producer_throughput / 0.1
        ) >= expected_ingress_rate, f"Observed producer throughput {producer_throughput} too low, expected: {expected_ingress_rate}"
        assert (
            consumer_throughput / 0.1
        ) >= expected_egress_rate, f"Observed consumer throughput {consumer_throughput} too low, expected: {expected_egress_rate}"


@contextmanager
def omb_runner(context, redpanda, driver, workload, omb_config):
    bench = OpenMessagingBenchmark(context, redpanda, driver,
                                   (workload, omb_config))
    # No need to set 'clean' flag as OMB service always cleans node on start
    # On nodes with lower perf start takes longer than 60 sec
    bench.start(timeout_sec=120)
    try:
        benchmark_time_min = bench.benchmark_time() + 1
        bench.wait(timeout_sec=benchmark_time_min * 60)
        yield bench
    except:
        redpanda.logger.exception("Exception within OMB")
        raise
    finally:
        bench.stop()


class HighThroughputTest(PreallocNodesTest):
    msg_size = 256 * KiB
    # Min segment size across all tiers
    min_segment_size = 16 * MiB
    unavailable_timeout = 60
    # default message count to check
    msg_count = 10000
    # Default value
    msg_timeout = 120

    def __init__(self, test_ctx: TestContext, *args, **kwargs):
        self._ctx = test_ctx
        # Get tier name
        self.config_profile_name = get_config_profile_name(
            RedpandaServiceCloud.get_cloud_globals(self._ctx.globals))
        extra_rp_conf = None
        num_brokers = None

        if self.config_profile_name == CloudTierName.DOCKER:
            # TODO: Bake the docker config into a higher layer that will
            # automatically load these settings upon call to make_rp_service
            num_brokers = 3
            extra_rp_conf = {
                'log_segment_size': 128 * MiB,
                'cloud_storage_cache_size': 20 * GiB,
                'kafka_connections_max': 100,
            }

        super(HighThroughputTest,
              self).__init__(test_ctx,
                             *args,
                             node_prealloc_count=3,
                             num_brokers=num_brokers,
                             extra_rp_conf=extra_rp_conf,
                             cloud_tier=self.config_profile_name,
                             disable_cloud_storage_diagnostics=True,
                             **kwargs)

        # Load install pack and check profile
        install_pack = self.redpanda.get_install_pack()
        self.logger.info(f"Loaded install pack '{install_pack['version']}': "
                         f"Redpanda v{install_pack['redpanda_version']}, "
                         f"created at '{install_pack['created_at']}'")
        if self.config_profile_name not in install_pack['config_profiles']:
            # throw user friendly error
            _profiles = ", ".join(
                [f"'{k}'" for k in install_pack['config_profiles']])
            raise RuntimeError(
                f"'{self.config_profile_name}' not found among config profiles: {_profiles}"
            )
        config_profile = install_pack['config_profiles'][
            self.config_profile_name]
        cluster_config = config_profile['cluster_config']

        self._num_brokers = config_profile['nodes_count']
        self._cluster_config_log_segment_size = int(
            cluster_config['log_segment_size'])
        self._memory_per_broker = MachineTypeMemory[
            config_profile['machine_type']]

        tier_product = self.redpanda.get_product()
        """
        The _partitions_upper_limit represents a rough value for the estimated
        maximum number of partitions that can be made on a new cluster via 1st
        create topics req.

        When attempting to issue create_topics request for the actual advertised
        maximum, the request may fail because per shard partition limits are
        exhausted. This may occur because other system topics may exist and the
        fact that the partition allocator isn't guaranteed to perfectly distribute
        the partitions across all shards evenly.
        """
        self._partitions_upper_limit = int(tier_product.max_partition_count *
                                           0.8)
        self._partitions_min = tier_product.max_partition_count // 50
        self._advertised_max_ingress = tier_product.max_ingress
        self._advertised_max_egress = tier_product.max_egress
        self._advertised_max_client_count = tier_product.max_client_count

        if self.config_profile_name == CloudTierName.DOCKER:
            si_settings = SISettings(
                test_ctx,
                log_segment_size=self.min_segment_size,
                cloud_storage_cache_size=cluster_config[
                    'cloud_storage_cache_size'],
            )
            self.redpanda.set_si_settings(si_settings)
            self.s3_port = si_settings.cloud_storage_api_endpoint_port

        test_ctx.logger.info(
            f"config profile {self.config_profile_name}: {config_profile}")

        self.rpk = RpkTool(self.redpanda)

        # Calculate timeout for 10000 messages with a 300% gap
        # Amount of data to push through is 2.5GB (10000 * 256KB)
        # 2500MB / 20MB (MB/sec, lowest tier) = 125 sec
        # reasonable timeout is 125 * 3 = 375 sec

        # By default we expect worst scenario when ingress rate
        # is affected by external factors, so half is used
        # dividing that by number of brokers to account for broker
        # traffic as well
        _half_ingress_rate = self._advertised_max_ingress / 2
        self.msg_timeout = int(
            self.msg_count /
            (_half_ingress_rate / self.msg_size / self._num_brokers))
        # Increase calculated timeout by 2
        self.msg_timeout *= 2
        # resources
        self.resources = []

    def _add_resource_tracking(self, type, resource):
        self.resources.append({"type": type, "spec": resource})

    def _create_topic_spec(self, partitions=None, replicas=None):
        # defaulting to max partitions
        _partitions = self._partitions_upper_limit if partitions is None else partitions
        _replicas = 3 if replicas is None else replicas
        _spec = TopicSpec(partition_count=_partitions,
                          replication_factor=_replicas,
                          retention_bytes=-1)
        self.topics = [_spec]
        self._add_resource_tracking("topic", _spec)

    def _create_default_topics(self, num_partitions=None, num_replicas=None):
        self._create_topic_spec(partitions=num_partitions,
                                replicas=num_replicas)
        self._create_initial_topics()

    def _clean_resources(self):
        for item in self.resources:
            if item['type'] == 'topic':
                self.rpk.delete_topic(item['spec'].name)

    @cluster(num_nodes=0)
    def test_cluster_cleanup(self):
        """
        This is not a test, but a cluster cleanup kicker
        """
        # Initiate cluster deletion
        # Cluster will be deleted if configuration is enabled it
        # and/or config.use_same_cluster and current.tests_finished set to True
        self.redpanda._cloud_cluster.current.tests_finished = True

    def tearDown(self):
        # These tests may run on cloud ec2 instances where between each test
        # the same cluster is used. Therefore state between runs will still exist,
        # remove the topic after each test run to clean out old state.
        if RedpandaServiceCloud.GLOBAL_CLOUD_CLUSTER_CONFIG in self._ctx.globals:
            self._clean_resources()

    def load_many_segments(self):
        """
        This methods intended use is to pre-load the cluster (and S3) with
        small segments to bootstrap a test enviornment that would stress
        the tiered storage subsytem.
        """
        cloud_segment_size = self.min_segment_size
        # Original value is 1000
        # which results in 1016 hours of data preload at tier1 ingress speed
        #   at '2' metric shows for 100% produced
        #     [INFO  - 2023-10-20 22:34:52,258 - si_utils - nodes_report_cloud_segments - lineno:539]: Cluster metrics report 315 / 800 cloud segments
        #   at '10'
        #     [INFO  - 2023-10-24 15:50:00,793 - kgo_verifier_services - _ingest_status - lineno:364]: Producer KgoVerifierProducer-0-140624622493344 progress: 69.58% ProduceStatus<2137520 2136495 0 0 0 0 115750.5/5504122.5/6040921.75>
        #     [INFO  - 2023-10-24 15:50:03,314 - si_utils - nodes_report_cloud_segments - lineno:539]: Cluster metrics report 2238 / 4000 cloud segments
        num_segments_per_partition = 100
        # Use half upper limit for projected number of segments in the cloud for 100% messages produced
        projected_cloud_segments = num_segments_per_partition * (
            self._partitions_upper_limit // 2)
        total_bytes_to_produce = projected_cloud_segments * cloud_segment_size
        total_messages = int((total_bytes_to_produce / self.msg_size) * 1.2)
        self.redpanda.logger.info(
            f"Total bytes: {total_bytes_to_produce}, "
            f"total messages: {total_messages}, "
            f"target segments: {projected_cloud_segments}")
        assert cloud_segment_size >= self.msg_size
        producer = KgoVerifierProducer(self.test_context, self.redpanda,
                                       self.topic, self.msg_size,
                                       total_messages)

        self.adjust_topic_segment_properties(cloud_segment_size,
                                             cloud_segment_size * 2)

        # Wait for at least as long as it would take to produce the data to the cluster
        # at the given tiers expected ingress rate. Add in a little more time to account
        # for any network hiccups or transitent errors
        estimated_produce_time_secs = int(
            (total_bytes_to_produce / self._advertised_max_ingress) * 1.2)
        # Use sensible target cloud segments number
        # based on ingress rate and amount of data can be produced in 10 min
        # on current tier
        target_cloud_segments = self._advertised_max_ingress // cloud_segment_size * 600 // 4
        try:
            producer.start()
            wait_until(lambda: nodes_report_cloud_segments(
                self.redpanda, target_cloud_segments),
                       timeout_sec=estimated_produce_time_secs,
                       backoff_sec=5)
        finally:
            producer.stop()
            producer.wait(timeout_sec=600)

        # Once some segments are generated, configure the topic to use more
        # realistic sizes.
        retention_bytes = int(self._advertised_max_ingress * 6 * hours /
                              self._partitions_upper_limit)
        # adjust log segment size to half of what is configured for the tier
        self.adjust_topic_segment_properties(
            self._cluster_config_log_segment_size // 2, retention_bytes)

    def adjust_topic_segment_properties(self, segment_bytes: int,
                                        retention_local_bytes: int):
        self.rpk.alter_topic_config(self.topic,
                                    TopicSpec.PROPERTY_SEGMENT_SIZE,
                                    segment_bytes)
        self.rpk.alter_topic_config(
            self.topic, TopicSpec.PROPERTY_RETENTION_LOCAL_TARGET_BYTES,
            retention_local_bytes)

    def get_node(self):
        idx = random.randrange(len(self.cluster.nodes))
        return self.cluster.nodes[idx]

    @cluster(num_nodes=2)
    def test_throughput_simple(self):
        # create default topics
        self._create_default_topics()
        # Generate traffic
        with traffic_generator(self.test_context, self.redpanda,
                               self._advertised_max_ingress,
                               self._advertised_max_egress, self.topic,
                               self.msg_size) as _:
            # Test will assert if advertised throughput isn't met
            time.sleep(15)

    def _get_swarm_connections_count(self, nodes):
        _list = []
        for sw_node in nodes:
            for node in sw_node.nodes:
                _current = int(
                    node.account.ssh_output(
                        "netstat -pan -t tcp | grep ESTABLISHED "
                        "| grep ':9092' | grep 'client-swarm' | wc -l").decode(
                            "utf-8").strip())
                _list.append(_current)
        return _list

    def _get_hwm_for_default_topic(self):
        _hwm = 0
        for partition in self.rpk.describe_topic(self.topic):
            # Add currect high watermark for topic
            _hwm += partition.high_watermark
        return _hwm

    @cluster(num_nodes=9)
    def test_max_connections(self):
        def calc_alive_swarm_nodes(swarm):
            _bAlive = []
            for snode in swarm:
                for node in snode.nodes:
                    _bAlive.append(True if snode.is_alive(node) else False)
            return sum(_bAlive)

        def swarm_start(node):
            # Simple delay in seconds before start
            # if delay_sec > 0:
            #     time.sleep(delay_sec)
            node.start()
            self.logger.warn("...started swarm node")
            return

        # Huge timeout for safety, 10 min
        # Most of the tiers will end in <5 min
        # Except for the 5-7 that will take >5 min up to 15 min
        FINISH_TIMEOUT_SEC = 900
        PRODUCER_TIMEOUT_MS = FINISH_TIMEOUT_SEC * 1000

        # setup ProducerSwarm parameters
        producer_kwargs = {}
        producer_kwargs['min_record_size'] = 64
        producer_kwargs['max_record_size'] = 64

        effective_msg_size = producer_kwargs['min_record_size'] + (
            producer_kwargs['max_record_size'] -
            producer_kwargs['min_record_size']) // 2

        # connections per node at max (tier-5) is ~3700
        # Account for the metadata traffic of 1%
        _target_total = int(self._advertised_max_client_count)
        _target_per_node = int(_target_total // len(self.cluster.nodes))
        _target_total = int(_target_total * 0.99)

        # No need to calculate message rate
        # Just use 1 msg per sec
        messages_per_sec_per_producer = 1
        # single producer runtime
        # Roughly every 500 connection needs 30 seconds to ramp up on 3 cluster nodes
        # 1000 -> on 6 cluster nodes
        _tier_coefficient = (_target_per_node // 1000) + (
            (_target_per_node % 1000) > 0)
        target_runtime_s = 30 * _tier_coefficient + 30
        # for 50 MiB total messag30e count is 3060
        records_per_producer = messages_per_sec_per_producer * target_runtime_s

        producer_kwargs[
            'messages_per_second_per_producer'] = messages_per_sec_per_producer

        # create default topics
        # Use lower partitions limit
        self._create_default_topics(num_partitions=self._partitions_min,
                                    num_replicas=3)

        # Initialize all 3 nodes with proper values
        swarm = []
        for idx in range(len(self.cluster.nodes), 0, -1):
            # First one will be longest, last shortest
            _swarm_node = ProducerSwarm(self._ctx,
                                        self.redpanda,
                                        self.topic,
                                        int(_target_per_node),
                                        int(records_per_producer),
                                        timeout_ms=PRODUCER_TIMEOUT_MS,
                                        **producer_kwargs)

            swarm.append(_swarm_node)

        # Start producing
        self.logger.warn(f"Start swarming from {len(swarm)} nodes: "
                         f"{_target_per_node} connections per node, "
                         f"{records_per_producer} msg each producer, "
                         f"{_target_total} total connections expected")
        # Total connections
        _total = 0
        connectMax = 0
        _start = time.time()
        # Swarm nodes will start in reverse order
        # Original thought was that index will serve as a delay
        # But that proves unnesessary as threading makes them
        # all start within a less than a second from each other
        idx = len(swarm)
        self.logger.warn(f"Starting {idx} swarm nodes")
        threads = []
        while idx > 0:
            # Create a thread with function that starts a swarm.
            # Threading here will not block other nodes from starting
            # No delay or anything needed
            t = Thread(target=swarm_start, args=(swarm[idx - 1], ))
            t.start()
            threads.append(t)
            # Next swarm node
            idx -= 1

        # Sync point for all threads
        for t in threads:
            t.join()

        # Track Connections
        _now = time.time()
        while (_now - _start) < 3600:
            _now = time.time()
            _connections = self._get_swarm_connections_count(swarm)
            _total = sum(_connections)
            _elapsed = "{:>5,.1f}s".format(_now - _start)
            self.logger.warn(f"{_elapsed}: {_total:>6} connections "
                             f"({'/'.join(map(str, _connections))}) ")
            # Save maximum
            connectMax = _total if connectMax < _total else connectMax
            # if at least half of the nodes finished, exit
            if len(swarm) / 2 >= calc_alive_swarm_nodes(swarm):
                break
            # sleep before next measurement
            time.sleep(30)

        # Message count is producers * total connections
        expected_msg_count = records_per_producer * int(
            self._advertised_max_client_count)
        # Since there is -1% on connections target set
        # Message count should be < expected * 0.99
        # or _target_total connections number * per producer
        reasonably_expected = records_per_producer * _target_total

        self.logger.warn("Waiting for messages")
        # Try and wait for all messages to be sent
        while (_now - _start) < FINISH_TIMEOUT_SEC:
            _alive = calc_alive_swarm_nodes(swarm)
            _total = len(swarm)
            _elapsed = "{:>5,.1f}s".format(_now - _start)
            _hwm = self._get_hwm_for_default_topic()
            _percent = _hwm / expected_msg_count * 100 if _hwm > 0 else 0
            self.logger.warn(f"{_elapsed}: Swarm nodes active: {_alive}, "
                             f"total: {_total}, msg produced: {_hwm} "
                             f"({_percent:.2f}%), "
                             f"expected: {expected_msg_count}")
            if _hwm > reasonably_expected:
                break
            # Wait for 1 min
            time.sleep(60)

        _now = time.time()
        _elapsed = "{:>5,.1f}s".format(_now - _start)
        self.logger.warn(f"Done swarming after {_elapsed}")

        # If timeout happen, just kill it
        self.logger.warn("Stopping swarm")
        for snode in swarm:
            for node in snode.nodes:
                if snode.is_alive(node):
                    snode.stop()

        # Assert that target connection count is reached
        self.logger.warn(f"Reached {connectMax} of {_target_total} needed")
        assert connectMax >= _target_total, \
            f"Expected >{_target_total} connections, actual {connectMax}"

        # Check message count
        _hwm = self._get_hwm_for_default_topic()

        # Check that all messages make it through
        _percent = _hwm / expected_msg_count * 100 if _hwm > 0 else 0
        _rpercent = reasonably_expected / expected_msg_count * 100
        self.logger.warn(f"Expected more than {reasonably_expected} "
                         f"({_rpercent}%) messages "
                         f"out of {expected_msg_count}, actual {_hwm} "
                         f"({_percent:.2f}%)")
        assert _hwm >= reasonably_expected, \
            f"Expected >{reasonably_expected} messages, actual {_hwm}"

        return

    COMBO_PRELOADED_LOG_ALLOW_LIST = [
        re.compile('storage - .* - Stopping parser, short read. .*')
    ] + RESTART_LOG_ALLOW_LIST

    @cluster(num_nodes=2, log_allow_list=COMBO_PRELOADED_LOG_ALLOW_LIST)
    def test_ht003_kgofailure(self):
        """
        Generates the maximum possible load to the cluster onto a topic that
        is replicated to the cloud, and then run various different test
        stages on the cluster:
        - rolling restart
        - stop and start single node, various scenarios
        - isolate and restore a node
        """
        # create default topics
        self._create_default_topics()

        # Generate a realistic number of segments per partition.
        with traffic_generator(self.test_context, self.redpanda,
                               self._advertised_max_ingress,
                               self._advertised_max_egress, self.topic,
                               self.msg_size) as tgen:
            # Wait until theres some traffic
            tgen.wait_for_traffic(acked=self.msg_count,
                                  timeout_sec=self.msg_timeout)

            # Run a rolling restart.
            self.stage_rolling_restart()

            # Hard stop, then restart.
            self.stage_stop_wait_start(forced_stop=True, downtime=0)

            # Stop a node, wait for enough time for movement to occur, then
            # restart.
            self.stage_stop_wait_start(forced_stop=False,
                                       downtime=self.unavailable_timeout)

            # Block traffic to/from one node.
            self.stage_block_node_traffic()

    NOS3_LOG_ALLOW_LIST = [
        re.compile("s3 - .* - Accessing .*, unexpected REST API error "
                   " detected, code: RequestTimeout"),
        re.compile("cloud_storage - .* - Exceeded cache size limit!"),
    ]

    # Stages for the "test_restarts"

    def stage_rolling_restart(self):
        self.logger.info(f"Rolling restarting nodes")
        self.redpanda.rolling_restart_nodes(self.redpanda.nodes,
                                            start_timeout=600,
                                            stop_timeout=600)

    def stage_block_node_traffic(self):
        wait_time = 120
        node = self.get_node()
        self.logger.info(f"Isolating node {node.name}")
        with FailureInjector(self.redpanda) as fi:
            fi.inject_failure(FailureSpec(FailureSpec.FAILURE_ISOLATE, node))
            self.logger.info(
                f"Running for {wait_time}s while failure injected")
            time.sleep(wait_time)

        self.logger.info(
            f"Running for {wait_time}s after injected failure removed")
        time.sleep(wait_time)
        self.logger.info(
            f"Waiting for the cluster to return to a healthy state")
        wait_until(self.redpanda.healthy, timeout_sec=600, backoff_sec=1)

    def stage_stop_wait_start(self, forced_stop: bool, downtime: int):
        node = self.get_node()
        self.logger.info(
            f"Stopping node {node.name} {'ungracefully' if forced_stop else 'gracefully'}"
        )
        self.redpanda.stop_node(node,
                                forced=forced_stop,
                                timeout=60 if forced_stop else 180)

        self.logger.info(f"Node downtime {downtime} s")
        time.sleep(downtime)

        restart_timeout = 300 + int(900 * downtime / 60)
        self.logger.info(
            f"Restarting node {node.name} for {restart_timeout} s")
        self.redpanda.start_node(node, timeout=600)
        wait_until(self.redpanda.healthy,
                   timeout_sec=restart_timeout,
                   backoff_sec=1)

    @cluster(num_nodes=2, log_allow_list=NOS3_LOG_ALLOW_LIST)
    def test_disrupt_cloud_storage(self):
        """
        Make segments replicate to the cloud, then disrupt S3 connectivity
        and restore it.

        Test designed with access to provider in mind and is
        for BYOC clouds only.
        Using it against FMC clouds will cause it to fail as
        there is no access to S3 account that is used inside
        cloudv2 API to create/update buckets and its
        properties/policies
        """
        # create default topics
        self._create_default_topics()

        segment_size = 16 * MiB  # Min segment size across all tiers
        self.adjust_topic_segment_properties(
            segment_bytes=segment_size, retention_local_bytes=segment_size)

        with traffic_generator(self.test_context, self.redpanda,
                               self._advertised_max_ingress,
                               self._advertised_max_egress, self.topic,
                               self.msg_size) as tgen:
            # Wait until theres some traffic
            tgen.wait_for_traffic(acked=self.msg_count,
                                  timeout_sec=self.msg_timeout)

            # S3 up -> down -> up
            self.stage_block_s3()

    def _cloud_storage_no_new_errors(self, redpanda, logger=None):
        num_errors = redpanda.metric_sum(
            "redpanda_cloud_storage_errors_total",
            metrics_endpoint=MetricsEndpoint.PUBLIC_METRICS)
        increase = (num_errors -
                    self.last_num_errors) if self.last_num_errors > 0 else 0
        self.last_num_errors = num_errors
        if logger:
            logger.info(
                f"Cluster metrics report {increase} new cloud storage errors ({num_errors} overall)"
            )
        return increase == 0

    def stage_block_s3(self):
        self.logger.info(f"Getting the first 100 segments into the cloud")
        wait_until(lambda: nodes_report_cloud_segments(self.redpanda, 100),
                   timeout_sec=600,
                   backoff_sec=5)
        self.logger.info(f"Blocking S3 traffic for all nodes")
        self.last_num_errors = 0

        if self.config_profile_name == CloudTierName.DOCKER:
            with firewall_blocked(self.redpanda.nodes, self.s3_port):
                # wait for the first cloud related failure + one minute
                wait_until(lambda: not self._cloud_storage_no_new_errors(
                    self.redpanda, self.logger),
                           timeout_sec=600,
                           backoff_sec=10)
                time.sleep(60)
        else:
            with cloudv2_object_store_blocked(self.redpanda, self.logger):
                # wait for the first cloud related failure + one minute
                wait_until(lambda: not self._cloud_storage_no_new_errors(
                    self.redpanda, self.logger),
                           timeout_sec=600,
                           backoff_sec=10)
                time.sleep(60)

        # make sure nothing is crashed
        wait_until(self.redpanda.healthy, timeout_sec=60, backoff_sec=1)
        self.logger.info(f"Waiting for S3 errors to cease")
        wait_until(lambda: self._cloud_storage_no_new_errors(
            self.redpanda, self.logger),
                   timeout_sec=600,
                   backoff_sec=20)

    def _wait_for_traffic(self, producer, acked, timeout=1800):
        try:
            self.logger.debug(
                f"Waiting until {acked} messages produced in {timeout}")
            wait_until(lambda: producer.produce_status.acked > acked,
                       timeout_sec=timeout,
                       backoff_sec=5.0)
        except TimeoutException as e:
            _throughput = producer.produce_status.acked * self.msg_size // timeout
            raise RuntimeError(
                f"Low throughput while preparing for the test: {_throughput}: {e}"
            )

    @ok_to_fail
    @cluster(num_nodes=5, log_allow_list=RESTART_LOG_ALLOW_LIST)
    def test_decommission_and_add(self):
        """
        Preloads cluster with large number of messages/segments that are also
        replicated to the cloud, and then runs the decommission-and-add a node
        stage. This could have been a part of 'test_combo_preloaded' but
        this stage alone is too heavy and long-lasting, so it's put into a
        separate test.
        """
        # create default topics
        self._create_default_topics()

        self.adjust_topic_segment_properties(
            segment_bytes=self.min_segment_size,
            retention_local_bytes=2 * self.min_segment_size)
        # Generate a realistic number of segments per partition.
        self.load_many_segments()
        try:
            producer = KgoVerifierProducer(
                self.test_context,
                self.redpanda,
                self.topic,
                msg_size=self.msg_size,
                msg_count=5_000_000_000_000,
                rate_limit_bps=self._advertised_max_ingress,
                custom_node=[self.preallocated_nodes[0]])

            producer.start()
            self._wait_for_traffic(producer,
                                   self.msg_count,
                                   timeout=self.msg_timeout)

            # Decommission.
            # Add node (after stage_decommission has freed up a node)
            # and wait for partitions to move in.
            self.stage_decommission_and_add()

        finally:
            producer.stop()
            producer.wait(timeout_sec=600)
            self.free_preallocated_nodes()

    def stage_decommission_and_add(self):
        node, node_id, node_str = self.get_node()

        def topic_partitions_on_node():
            try:
                parts = self.redpanda.partitions(self.topic)
            except StopIteration:
                return 0
            n = sum([
                1 if r.account == node.account else 0 for p in parts
                for r in p.replicas
            ])
            self.logger.debug(f"Partitions in the node-topic: {n}")
            return n

        # create default topics
        self._create_default_topics()

        nt_partitions_before = topic_partitions_on_node()

        self.logger.info(
            f"Decommissioning node {node_str}, partitions: {nt_partitions_before}"
        )
        decomm_time = time.monotonic()
        admin = self.redpanda._admin
        admin.decommission_broker(node_id)
        waiter = NodeDecommissionWaiter(self.redpanda,
                                        node_id,
                                        self.logger,
                                        progress_timeout=120)
        waiter.wait_for_removal()
        self.redpanda.stop_node(node)
        assert topic_partitions_on_node() == 0
        decomm_time = time.monotonic() - decomm_time

        self.logger.info(f"Adding a node")
        self.redpanda.clean_node(node,
                                 preserve_logs=True,
                                 preserve_current_install=True)
        self.redpanda.start_node(node,
                                 auto_assign_node_id=False,
                                 omit_seeds_on_idx_one=False)
        wait_until(self.redpanda.healthy, timeout_sec=600, backoff_sec=1)
        new_node_id = self.redpanda.node_id(node, force_refresh=True)

        self.logger.info(
            f"Node added, new node_id: {new_node_id}, waiting for {int(nt_partitions_before/2)} partitions to move there in {int(decomm_time*2)} s"
        )
        wait_until(
            lambda: topic_partitions_on_node() > nt_partitions_before / 2,
            timeout_sec=max(120, decomm_time * 2),
            backoff_sec=2,
            err_msg=
            f"{int(nt_partitions_before/2)} partitions failed to move to node {new_node_id} in {max(60, decomm_time*2)} s"
        )
        self.logger.info(f"{topic_partitions_on_node()} partitions moved")

    @cluster(num_nodes=3, log_allow_list=NOS3_LOG_ALLOW_LIST)
    def test_cloud_cache_thrash(self):
        """
        Try to exhaust cloud cache by reading at random offsets with many
        consumers
        """
        # create default topics
        self._create_default_topics()

        segment_size = 16 * MiB  # Min segment size across all tiers
        self.adjust_topic_segment_properties(
            segment_bytes=segment_size, retention_local_bytes=segment_size)

        with traffic_generator(self.test_context, self.redpanda,
                               self._advertised_max_ingress,
                               self._advertised_max_egress, self.topic,
                               self.msg_size) as tgen:
            tgen.wait_for_traffic(acked=self.msg_count,
                                  timeout_sec=self.msg_timeout)
            wait_until(lambda: nodes_report_cloud_segments(self.redpanda, 100),
                       timeout_sec=600,
                       backoff_sec=5)
            tgen._producer.wait_for_offset_map()

            # Exhaust cloud cache with multiple consumers
            # reading at random offsets
            self.logger.info(f"Starting thrashing consumers")
            consumer = KgoVerifierRandomConsumer(self.test_context,
                                                 self.redpanda,
                                                 self.topic,
                                                 msg_size=self.msg_size,
                                                 rand_read_msgs=1,
                                                 parallel=4,
                                                 debug_logs=True)
            try:
                consumer.start(clean=False)
                time.sleep(240)
            finally:
                self.logger.info(f"Stopping thrashing consumers")
                consumer.stop()
                consumer.wait(timeout_sec=600)

    @cluster(num_nodes=7, log_allow_list=RESTART_LOG_ALLOW_LIST)
    def test_consume(self):
        # create default topics
        self._create_default_topics()

        self.adjust_topic_segment_properties(
            segment_bytes=self.min_segment_size,
            retention_local_bytes=2 * self.min_segment_size)

        # Generate a realistic number of segments per partition.
        self.load_many_segments()
        producer = None
        try:
            producer = KgoVerifierProducer(
                self.test_context,
                self.redpanda,
                self.topic,
                msg_size=self.msg_size,
                msg_count=5_000_000_000_000,
                rate_limit_bps=self._advertised_max_ingress,
                custom_node=[self.preallocated_nodes[0]])

            producer.start()
            self._wait_for_traffic(producer,
                                   self.msg_count,
                                   timeout=self.msg_timeout)

            self.stage_lots_of_failed_consumers()
            self.stage_hard_restart(producer)

        finally:
            producer.stop()
            producer.wait(timeout_sec=600)
            self.free_preallocated_nodes()

    @ignore
    @cluster(num_nodes=10, log_allow_list=RESTART_LOG_ALLOW_LIST)
    def test_ts_resource_utilization(self):
        """
        In the AWS EC2 CDT testing env, this test is designed for
        at least three is4gen.4xlarge or it will not achieve the
        desired throughput to drive resource utilization.
        """
        self._create_topic_spec()
        self.stage_tiered_storage_consuming()

    def stage_lots_of_failed_consumers(self):
        # This stage sequentially starts 1,000 consumers. Then allows
        # them to consume ~10 messages before terminating them with
        # a SIGKILL.

        self.logger.info(f"Starting stage_lots_of_failed_consumers")
        # Original value 10000
        consume_count = 5000

        def random_stop_check(consumer):
            # original step was 10 messages
            if consumer.message_count >= min(50, consume_count):
                return True
            else:
                return False

        while consume_count > 0:
            consumer = RpkConsumer(self._ctx,
                                   self.redpanda,
                                   self.topic,
                                   num_msgs=consume_count)
            consumer.start()
            wait_until(lambda: random_stop_check(consumer),
                       timeout_sec=30,
                       backoff_sec=0.001)

            consumer.stop()
            consumer.free()

            consume_count -= consumer.message_count
            self.logger.warn(f"consumed {consumer.message_count} messages, "
                             f"{consume_count} left")

    def _consume_from_offset(self, topic_name: str, msg_count: int,
                             partition: int, starting_offset: str,
                             timeout_per_topic: int):
        def consumer_saw_msgs(consumer):
            self.logger.info(
                f"Consumer message_count={consumer.message_count} / {msg_count}"
            )
            # Tolerate greater-than, because if there were errors during production
            # there can have been retries.
            return consumer.message_count >= msg_count

        consumer = RpkConsumer(self._ctx,
                               self.redpanda,
                               topic_name,
                               save_msgs=True,
                               partitions=[partition],
                               offset=starting_offset,
                               num_msgs=msg_count)
        consumer.start()
        wait_until(lambda: consumer_saw_msgs(consumer),
                   timeout_sec=timeout_per_topic,
                   backoff_sec=0.125)

        consumer.stop()
        consumer.free()

        if starting_offset.isnumeric():
            expected_offset = int(starting_offset)
            actual_offset = int(consumer.messages[0]['offset'])
            assert expected_offset == actual_offset, "expected_offset != actual_offset"

    # The testcase occasionally fails on various parts:
    # - toing on `_consume_from_offset(self.topic, 1, p_id, "newest", 30)`
    # - failing to ensure all manifests are in the cloud in `stop_and_scrub_object_storage`
    @ignore
    @cluster(num_nodes=7, log_allow_list=RESTART_LOG_ALLOW_LIST)
    def test_consume_miss_cache(self):
        # create default topics
        self._create_default_topics()

        self.adjust_topic_segment_properties(
            segment_bytes=self.min_segment_size,
            retention_local_bytes=2 * self.min_segment_size)

        # Generate a realistic number of segments per partition.
        self.load_many_segments()
        producer = None
        try:
            producer = KgoVerifierProducer(
                self.test_context,
                self.redpanda,
                self.topic,
                msg_size=self.msg_size,
                msg_count=5 * 1024 * 1024 * 1024 * 1024,
                rate_limit_bps=self._advertised_max_ingress,
                custom_node=[self.preallocated_nodes[0]])

            producer.start()
            self._wait_for_traffic(producer,
                                   self.msg_count,
                                   timeout=self.msg_timeout)

            # this stage could have been a part of test_consume however one
            # of the checks requires nicely balanced replicas, and this is
            # something other test_consume stages could break
            self.stage_consume_miss_cache(producer)

        finally:
            producer.stop()
            producer.wait(timeout_sec=600)
            self.free_preallocated_nodes()

    def stage_consume_miss_cache(self, producer: KgoVerifierProducer):
        # This stage produces enough data to each RP node to fill up their batch caches
        # for a specific topic. Then it consumes from offsets known to not be in the
        # batch and verifies that these fetches occurred from disk and not the cache.

        self.logger.info(f"Starting stage_consume_miss_cache")

        # Get current offsets for topic. We'll use these for starting offsets
        # for consuming messages after we produce enough data to push them out
        # of the batch cache.
        last_offsets = [(p.id, p.high_watermark)
                        for p in self.rpk.describe_topic(self.topic)
                        if p.high_watermark is not None]

        partition_size_check: list[MetricCheck] = []
        partition_size_metric = "vectorized_storage_log_partition_size"

        for node in self.redpanda.nodes:
            partition_size_check.append(
                MetricCheck(self.logger,
                            self.redpanda,
                            node,
                            partition_size_metric,
                            reduce=sum))

        # wait for producer to produce enough to exceed the batch cache by some margin
        # For a test on 4x `is4gen.4xlarge` there is about 0.13 GiB/s of throughput per node.
        # This would mean we'd be waiting 90GiB / 0.13 GiB/s = 688s or 11.5 minutes to ensure
        # the cache has been filled by the producer.
        produce_rate_per_node_bytes_s = self._advertised_max_ingress / self._num_brokers
        batch_cache_max_memory = self._memory_per_broker
        time_till_memory_full_per_node = batch_cache_max_memory / produce_rate_per_node_bytes_s
        required_wait_time_s = 1.5 * time_till_memory_full_per_node

        self.logger.info(
            f"Expecting to wait {required_wait_time_s} seconds. "
            f"time_till_memory_full_per_node: {time_till_memory_full_per_node}, "
            f"produce_rate_per_node_bytes_s: {produce_rate_per_node_bytes_s}")

        current_sent = producer.produce_status.sent
        expected_sent = math.ceil(
            (self._num_brokers * batch_cache_max_memory) / self.msg_size)

        self.logger.info(
            f"{current_sent} currently sent messages. Waiting for {expected_sent} messages to be sent"
        )

        def producer_complete():
            number_left = (current_sent +
                           expected_sent) - producer.produce_status.sent
            self.logger.info(f"{number_left} messages still need to be sent.")
            return number_left <= 0

        try:
            wait_until(producer_complete,
                       timeout_sec=required_wait_time_s,
                       backoff_sec=30)
        except Exception as e:
            _percent = (producer.produce_status.sent * 100) / expected_sent
            self.log.warning("# Timeout waiting for all messages: "
                             f"expected {expected_sent}, "
                             f"current {producer.produce_status.sent} "
                             f"({_percent}%)\n{e}")

        post_prod_offsets = [(p.id, p.high_watermark)
                             for p in self.rpk.describe_topic(self.topic)
                             if p.high_watermark is not None]

        offset_deltas = [
            (c[0][0], c[0][1] - c[1][1])
            for c in list(itertools.product(post_prod_offsets, last_offsets))
            if c[0][0] == c[1][0]
        ]
        avg_delta = sum([d[1] for d in offset_deltas]) / len(offset_deltas)

        self.logger.info(
            f"Finished waiting for batch cache to fill. Avg log offset delta: {avg_delta}"
        )

        # Report under-replicated status for the context in the case the following check fails
        s = self.redpanda.metrics_sample(
            "vectorized_cluster_partition_under_replicated_replicas")
        if sum(x.value for x in s.samples) == 0:
            self.logger.info(f"No under-replicated replicas")
        else:
            self.logger.info(f"Under-replicated replicas: {s.samples}")

        # Ensure total partition size increased by the amount expected.
        for check in partition_size_check:

            def check_partition_size(old_size, new_size):
                self.logger.info(
                    f"Total increase in size for partitions: {new_size-old_size}, "
                    f"checked from {check.node.name}")

                unreplicated_size_inc = (new_size - old_size) / 3
                return unreplicated_size_inc >= batch_cache_max_memory

            check.expect([(partition_size_metric, check_partition_size)])

        # Ensure there are metrics available for the topic we're consuming from
        for p_id, _ in last_offsets:
            self._consume_from_offset(self.topic, 1, p_id, "newest", 30)

        # Stop the producer temporarily as produce requests can cause
        # batch cache reads which causes false negatives on cache misses.
        producer.stop()

        check_batch_cache_reads = []
        cache_metrics = [
            "vectorized_storage_log_read_bytes_total",
            "vectorized_storage_log_cached_read_bytes_total",
        ]

        for node in self.redpanda.nodes:
            check_batch_cache_reads.append(
                MetricCheck(self.logger,
                            self.redpanda,
                            node,
                            cache_metrics,
                            reduce=sum))

        # start consuming at the offsets recorded before the wait.
        # at this point we can be sure they are not from the batch cache.
        messages_to_read = 1
        timeout_seconds = 60

        for p_id, p_hw in last_offsets:
            self._consume_from_offset(self.topic, messages_to_read, p_id,
                                      str(p_hw), timeout_seconds)

        def check_cache_bytes_ratio(old, new):
            log_diff = int(new[cache_metrics[0]]) - int(old[cache_metrics[0]])
            cache_diff = int(new[cache_metrics[1]]) - int(
                old[cache_metrics[1]])
            cache_hit_percent = cache_diff / log_diff

            self.logger.info(
                f"BYTES: log_diff: {log_diff} cache_diff: {cache_diff} cache_hit_percent: {cache_hit_percent}"
            )
            return cache_hit_percent <= 0.4

        for check in check_batch_cache_reads:
            ok = check.evaluate_groups([(cache_metrics,
                                         check_cache_bytes_ratio)])

            assert ok, "cache hit ratio is higher than expected"

    def _run_omb(self, produce_bps,
                 validator_overrides) -> OpenMessagingBenchmark:
        topic_count = 1
        partitions_per_topic = self._partitions_upper_limit
        workload = {
            "name": "StabilityTest",
            "topics": topic_count,
            "partitions_per_topic": partitions_per_topic,
            "subscriptions_per_topic": 1,
            "consumer_per_subscription": 2,
            "producers_per_topic": 2,
            "producer_rate": int(produce_bps / (4 * KiB)),
            "message_size": 4 * KiB,
            "payload_file": "payload/payload-4Kb.data",
            "consumer_backlog_size_GB": 0,
            "test_duration_minutes": 3,
            "warmup_duration_minutes": 1,
        }

        bench_node = self.preallocated_nodes[0]
        worker_nodes = self.preallocated_nodes[1:]

        benchmark = OpenMessagingBenchmark(
            self._ctx, self.redpanda, "SIMPLE_DRIVER",
            (workload, OMBSampleConfigurations.UNIT_TEST_LATENCY_VALIDATOR
             | validator_overrides))

        benchmark.start()
        return benchmark

    def stage_tiered_storage_consuming(self):
        # This stage starts two consume + produce workloads concurrently.
        # One workload being a usual one without tiered storage that is
        # consuming entirely from the batch cache. The other being a outlier
        # where the consumer is consuming entirely from S3. The stage then
        # ensures that the S3 workload doesn't impact the performance of the
        # usual workload too greatly.

        self.logger.info(f"Starting stage_tiered_storage_consuming")

        segment_size = 128 * MiB  # 128 MiB
        consume_rate = 1 * GiB  # 1 GiB/s

        # create a new topic with low local retention.
        config = {
            'segment.bytes': segment_size,
            'retention.bytes': -1,
            'retention.local.target.bytes': 2 * segment_size,
            'cleanup.policy': 'delete',
            'partition_autobalancing_node_availability_timeout_sec':
            self.unavailable_timeout,
            'partition_autobalancing_mode': 'continuous',
            'raft_learner_recovery_rate': 10 * GiB,
        }
        self.rpk.create_topic(self.topic,
                              partitions=self._partitions_upper_limit,
                              replicas=3,
                              config=config)

        producer = KgoVerifierProducer(
            self.test_context,
            self.redpanda,
            self.topic,
            msg_size=self.msg_size,
            msg_count=5_000_000_000_000,
            rate_limit_bps=self._advertised_max_ingress)
        producer.start()

        # produce 10 mins worth of consume data onto S3.
        produce_time_s = 4 * 60
        messages_to_produce = (produce_time_s * consume_rate) / self.msg_size
        time_to_wait = (messages_to_produce *
                        self.msg_size) / self._advertised_max_ingress

        wait_until(
            lambda: producer.produce_status.acked >= messages_to_produce,
            timeout_sec=1.5 * time_to_wait,
            backoff_sec=5,
            err_msg=
            f"Could not ack production of {messages_to_produce} messages in {1.5 * time_to_wait} s"
        )
        # continue to produce the rest of the test

        validator_overrides = {
            OMBSampleConfigurations.E2E_LATENCY_50PCT:
            [OMBSampleConfigurations.lte(51)],
            OMBSampleConfigurations.E2E_LATENCY_AVG:
            [OMBSampleConfigurations.lte(145)],
        }

        # Run a usual producer + consumer workload and a S3 producer + consumer workload concurrently
        # Ensure that the S3 workload doesn't effect the usual workload majorly.
        benchmark = self._run_omb(self._advertised_max_ingress / 2,
                                  validator_overrides)

        # This consumer should largely be reading from S3
        consumer = RpkConsumer(self._ctx,
                               self.redpanda,
                               self.topic,
                               offset="oldest",
                               num_msgs=messages_to_produce)
        consumer.start()
        wait_until(
            lambda: consumer.message_count >= messages_to_produce,
            timeout_sec=5 * produce_time_s,
            backoff_sec=5,
            err_msg=
            f"Could not consume {messages_to_produce} msgs in {5 * produce_time_s} s"
        )
        consumer.stop()
        consumer.free()

        benchmark_time_min = benchmark.benchmark_time() + 5
        benchmark.wait(timeout_sec=benchmark_time_min * 60)
        benchmark.check_succeed()

    def stage_hard_restart(self, producer):
        # This stage force stops all Redpanda nodes. It then
        # starts them all again and verifies the cluster is
        # healthy. Afterwards is runs some basic produce + consume
        # operations.

        self.logger.info(f"Starting stage_hard_restart")

        # hard stop all nodes
        self.logger.info("stopping all redpanda nodes")
        for node in self.redpanda.nodes:
            self.redpanda.stop_node(node, forced=True)

        # start all nodes again
        self.logger.info("starting all redpanda nodes")
        for node in self.redpanda.nodes:
            self.redpanda.start_node(node, timeout=600)

        # wait until the cluster is health once more
        self.logger.info("waiting for RP cluster to be healthy")
        wait_until(self.redpanda.healthy, timeout_sec=900, backoff_sec=1)

        # verify basic produce and consume operations still work.

        self.logger.info("checking basic producer functions")
        current_sent = producer.produce_status.sent
        produce_count = 100

        def producer_complete():
            number_left = (current_sent +
                           produce_count) - producer.produce_status.sent
            self.logger.info(f"{number_left} messages still need to be sent.")
            return number_left <= 0

        wait_until(producer_complete, timeout_sec=120, backoff_sec=1)

        self.logger.info("checking basic consumer functions")
        current_sent = producer.produce_status.sent
        consume_count = 100
        consumer = RpkConsumer(self._ctx,
                               self.redpanda,
                               self.topic,
                               offset="newest",
                               num_msgs=consume_count)
        consumer.start()
        wait_until(lambda: consumer.message_count >= consume_count,
                   timeout_sec=120,
                   backoff_sec=1,
                   err_msg=f"Could not consume {consume_count} msgs in 1 min")

        consumer.stop()
        consumer.free()

    def _prepare_omb_workload(self, ramp_time, duration, partitions, rate,
                              msg_size, producers, consumers):
        return {
            "name": "HT004-MINPARTOMB",
            "topics": 1,
            "partitions_per_topic": partitions,
            "subscriptions_per_topic": 1,
            "consumer_per_subscription": consumers,
            "producers_per_topic": producers,
            "producer_rate": rate,
            "message_size": msg_size,
            "consumer_backlog_size_GB": 0,
            "test_duration_minutes": duration,
            "warmup_duration_minutes": ramp_time,
            "use_randomized_payloads": True,
            "random_bytes_ratio": 0.5,
            "randomized_payload_pool_size": 100,
        }

    @cluster(num_nodes=6, log_allow_list=RESTART_LOG_ALLOW_LIST)
    @parametrize(partitions="min")
    @parametrize(partitions="max")
    def test_htt_partitions_omb(self, partitions):
        def _format_metrics(tier):
            return "\n".join([f"{k} = {v} " for k, v in tier.items()])

        def _get_metrics(bench):
            return list(
                json.loads(bench.node.account.ssh_output(
                    bench.chart_cmd)).values())[0]

        # Get values for almost idle cluster load
        rampup_time = 1
        runtime = 30
        msg_size = 16 * KiB
        rate = self._advertised_max_ingress // msg_size
        producers = 1 * (self._num_brokers // 3) + 1
        consumers = producers * 2

        if partitions not in ["min", "max"]:
            raise RuntimeError("Test parameter for partitions invalid")

        # Calculate target throughput latencies
        target_e2e_50pct = 51
        target_e2e_avg = 145

        # Measure with target load
        validator_overrides = {
            OMBSampleConfigurations.E2E_LATENCY_50PCT:
            [OMBSampleConfigurations.lte(target_e2e_50pct)],
            OMBSampleConfigurations.E2E_LATENCY_AVG:
            [OMBSampleConfigurations.lte(target_e2e_avg)],
        }
        # Select number of partitions
        if partitions == "min":
            _num_partitions = self._partitions_min
        elif partitions == "max":
            _num_partitions = self._partitions_upper_limit

        workload = self._prepare_omb_workload(rampup_time, runtime,
                                              _num_partitions, rate, msg_size,
                                              producers, consumers)
        with omb_runner(
                self._ctx, self.redpanda, "SIMPLE_DRIVER", workload,
                OMBSampleConfigurations.UNIT_TEST_LATENCY_VALIDATOR
                | validator_overrides) as omb:
            metrics = _get_metrics(omb)
            # Tier metrics should not diviate from idle
            # metrics more than 145 ms on the average
            self.logger.info(f"Workload metrics: {_format_metrics(metrics)}")
            # Assert test results
            omb.check_succeed()
