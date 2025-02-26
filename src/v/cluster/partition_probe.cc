// Copyright 2020 Redpanda Data, Inc.
//
// Use of this software is governed by the Business Source License
// included in the file licenses/BSL.md
//
// As of the Change Date specified in that file, in accordance with
// the Business Source License, use of this software will be governed
// by the Apache License, Version 2.0

#include "cluster/partition_probe.h"

#include "cluster/partition.h"
#include "config/configuration.h"
#include "metrics/metrics.h"
#include "model/metadata.h"
#include "pandaproxy/schema_registry/schema_id_validation.h"
#include "prometheus/prometheus_sanitize.h"

#include <seastar/core/metrics.hh>

namespace cluster {

replicated_partition_probe::replicated_partition_probe(
  const partition& p) noexcept
  : _partition(p) {
    config::shard_local_cfg().enable_schema_id_validation.bind().watch(
      [this]() { reconfigure_metrics(); });
}

void replicated_partition_probe::reconfigure_metrics() {
    clear_metrics();
    setup_metrics(_partition.ntp());
}

void replicated_partition_probe::clear_metrics() {
    _metrics.clear();
    _public_metrics.clear();
}

void replicated_partition_probe::setup_metrics(const model::ntp& ntp) {
    setup_internal_metrics(ntp);
    setup_public_metrics(ntp);
}

void replicated_partition_probe::setup_internal_metrics(const model::ntp& ntp) {
    namespace sm = ss::metrics;

    if (config::shard_local_cfg().disable_metrics()) {
        return;
    }

    auto ns_label = sm::label("namespace");
    auto topic_label = sm::label("topic");
    auto partition_label = sm::label("partition");

    const std::vector<sm::label_instance> labels = {
      ns_label(ntp.ns()),
      topic_label(ntp.tp.topic()),
      partition_label(ntp.tp.partition()),
    };

    _metrics.add_group(
      prometheus_sanitize::metrics_name("cluster:partition"),
      {
        sm::make_gauge(
          "leader",
          [this] { return _partition.is_elected_leader() ? 1 : 0; },
          sm::description(
            "Flag indicating if this partition instance is a leader"),
          labels),
        sm::make_gauge(
          "start_offset",
          [this] { return _partition.raft_start_offset(); },
          sm::description("start offset"),
          labels),
        sm::make_gauge(
          "last_stable_offset",
          [this] { return _partition.last_stable_offset(); },
          sm::description("Last stable offset"),
          labels),
        sm::make_gauge(
          "committed_offset",
          [this] { return _partition.committed_offset(); },
          sm::description("Partition commited offset. i.e. safely persisted on "
                          "majority of replicas"),
          labels),
        sm::make_gauge(
          "end_offset",
          [this] { return _partition.dirty_offset(); },
          sm::description(
            "Last offset stored by current partition on this node"),
          labels),
        sm::make_gauge(
          "high_watermark",
          [this] { return _partition.high_watermark(); },
          sm::description(
            "Partion high watermark i.e. highest consumable offset"),
          labels),
        sm::make_gauge(
          "leader_id",
          [this] {
              return _partition.raft()->get_leader_id().value_or(
                model::node_id(-1));
          },
          sm::description("Id of current partition leader"),
          labels),
        sm::make_gauge(
          "under_replicated_replicas",
          [this] {
              return _partition.raft()->get_under_replicated().value_or(0);
          },
          sm::description("Number of under replicated replicas"),
          labels),
        sm::make_counter(
          "records_produced",
          [this] { return _records_produced; },
          sm::description("Total number of records produced"),
          labels),
        sm::make_counter(
          "records_fetched",
          [this] { return _records_fetched; },
          sm::description("Total number of records fetched"),
          labels),
        sm::make_total_bytes(
          "bytes_produced_total",
          [this] { return _bytes_produced; },
          sm::description("Total number of bytes produced"),
          labels),
        sm::make_total_bytes(
          "bytes_fetched_total",
          [this] { return _bytes_fetched; },
          sm::description("Total number of bytes fetched"),
          labels),
        sm::make_total_bytes(
          "cloud_storage_segments_metadata_bytes",
          [this] {
              return _partition.archival_meta_stm()
                       ? _partition.archival_meta_stm()
                           ->manifest()
                           .segments_metadata_bytes()
                       : 0;
          },
          sm::description("Current number of bytes consumed by remote segments "
                          "managed for this partition"),
          labels),
      },
      {},
      {sm::shard_label});

    if (
      config::shard_local_cfg().enable_schema_id_validation()
      != pandaproxy::schema_registry::schema_id_validation_mode::none) {
        _metrics.add_group(
          prometheus_sanitize::metrics_name("cluster:partition"),
          {
            sm::make_counter(
              "schema_id_validation_records_failed",
              [this] { return _schema_id_validation_records_failed; },
              sm::description(
                "Number of records that failed schema ID validation"),
              labels)
              .aggregate({sm::shard_label, partition_label}),
          });
    }
}

void replicated_partition_probe::setup_public_metrics(const model::ntp& ntp) {
    namespace sm = ss::metrics;

    if (config::shard_local_cfg().disable_public_metrics()) {
        return;
    }

    auto request_label = metrics::make_namespaced_label("request");
    auto ns_label = metrics::make_namespaced_label("namespace");
    auto topic_label = metrics::make_namespaced_label("topic");
    auto partition_label = metrics::make_namespaced_label("partition");

    const std::vector<sm::label_instance> labels = {
      ns_label(ntp.ns()),
      topic_label(ntp.tp.topic()),
      partition_label(ntp.tp.partition()),
    };

    _public_metrics.add_group(
      prometheus_sanitize::metrics_name("kafka"),
      {
        // Partition Level Metrics
        sm::make_gauge(
          "max_offset",
          [this] {
              auto log_offset = _partition.high_watermark();
              auto translator = _partition.get_offset_translator_state();

              try {
                  return translator->from_log_offset(log_offset);
              } catch (std::runtime_error& e) {
                  // Offset translation will throw if nothing was committed
                  // to the partition or if the offset is outside the
                  // translation range for any other reason.
                  return model::offset(-1);
              }
          },
          sm::description(
            "Latest readable offset of the partition (i.e. high watermark)"),
          labels)
          .aggregate({sm::shard_label}),
        sm::make_gauge(
          "under_replicated_replicas",
          [this] {
              auto metrics = _partition.raft()->get_follower_metrics();
              return std::count_if(
                metrics.cbegin(),
                metrics.cend(),
                [](const raft::follower_metrics& fm) {
                    return fm.under_replicated;
                });
          },
          sm::description("Number of under replicated replicas (i.e. replicas "
                          "that are live, but not at the latest offest)"),
          labels)
          .aggregate({sm::shard_label}),
        // Topic Level Metrics
        sm::make_total_bytes(
          "request_bytes_total",
          [this] { return _bytes_produced; },
          sm::description("Total number of bytes produced per topic"),
          {request_label("produce"),
           ns_label(ntp.ns()),
           topic_label(ntp.tp.topic()),
           partition_label(ntp.tp.partition())})
          .aggregate({sm::shard_label, partition_label}),
        sm::make_total_bytes(
          "request_bytes_total",
          [this] { return _bytes_fetched; },
          sm::description("Total number of bytes consumed per topic"),
          {request_label("consume"),
           ns_label(ntp.ns()),
           topic_label(ntp.tp.topic()),
           partition_label(ntp.tp.partition())})
          .aggregate({sm::shard_label, partition_label}),
        sm::make_counter(
          "records_produced_total",
          [this] { return _records_produced; },
          sm::description("Total number of records produced"),
          labels)
          .aggregate({sm::shard_label, partition_label}),
        sm::make_counter(
          "records_fetched_total",
          [this] { return _records_fetched; },
          sm::description("Total number of records fetched"),
          labels)
          .aggregate({sm::shard_label, partition_label}),
      });
    if (
      config::shard_local_cfg().enable_schema_id_validation()
      != pandaproxy::schema_registry::schema_id_validation_mode::none) {
        _public_metrics.add_group(
          prometheus_sanitize::metrics_name("cluster:partition"),
          {
            sm::make_counter(
              "schema_id_validation_records_failed",
              [this] { return _schema_id_validation_records_failed; },
              sm::description(
                "Number of records that failed schema ID validation"),
              labels)
              .aggregate({sm::shard_label, partition_label}),
          });
    }
}

} // namespace cluster
