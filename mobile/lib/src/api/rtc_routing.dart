import 'dart:async';
import 'dart:io';

import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';

/// Credential-free HTTPS RTT probes. Failed measurements are omitted.
Future<Map<String, int>> measureRtcLatency(List<Object?> targets) async {
  final result = <String, int>{};
  final deadline = Stopwatch()..start();
  var index = 0;
  Future<void> worker() async {
    final client = HttpClient()
      ..connectionTimeout = const Duration(milliseconds: 1500);
    try {
      while (index < targets.length &&
          index < 64 &&
          deadline.elapsedMilliseconds < 8000) {
        final target = targets[index++];
        if (target is! Map) continue;
        final region = target['region'];
        final url = Uri.tryParse('${target['probe_url']}');
        if (region is! String ||
            !RegExp(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
                .hasMatch(region) ||
            url == null ||
            url.scheme != 'https' ||
            url.userInfo.isNotEmpty ||
            url.port != 443 ||
            url.path != '/rtc-probe' ||
            url.hasQuery ||
            url.hasFragment) {
          continue;
        }
        final samples = <int>[];
        for (var sample = 0;
            sample < 4 && deadline.elapsedMilliseconds < 8000;
            sample++) {
          HttpClientRequest? request;
          var expired = false;
          final measuredSample = sample;
          final watch = Stopwatch()..start();
          final remaining = 8000 - deadline.elapsedMilliseconds;
          try {
            await (() async {
              request = await client.getUrl(url);
              if (expired) {
                request!.abort();
                return;
              }
              request!.followRedirects = false;
              request!.headers.set(HttpHeaders.cacheControlHeader, 'no-store');
              final response = await request!.close();
              if (!expired &&
                  measuredSample > 0 &&
                  response.statusCode == 204 &&
                  response.headers.value('x-cinnamon-rtc-probe') == '1') {
                samples.add(watch.elapsedMicroseconds);
              }
              await response.drain<void>();
            })()
                .timeout(Duration(
                    milliseconds: remaining < 1500 ? remaining : 1500));
          } on Object {
            expired = true;
            request?.abort();
          }
        }
        if (samples.length >= 2) {
          samples.sort();
          final middle = samples.length ~/ 2;
          final median = samples.length.isOdd
              ? samples[middle].toDouble()
              : (samples[middle - 1] + samples[middle]) / 2;
          result[region] = (median / 1000).ceil().clamp(1, 60000);
        }
      }
    } finally {
      client.close(force: true);
    }
  }

  await Future.wait(
      List.generate(targets.length < 6 ? targets.length : 6, (_) => worker()));
  return result;
}

extension RtcRoutingRepository on KaedeRepository {
  Future<Map<String, Object?>> rtcRouting(
          {EntityRef? channel, EntityRef? call}) =>
      api.getJson(
        '/api/v1/voice/rtc',
        query: {
          if (call != null) 'call_ref': call.wire,
          if (call == null && channel != null) 'channel_ref': channel.wire,
        },
      );

  Future<Map<String, Object?>?> rtcHints(
      {EntityRef? channel, EntityRef? call, String? region}) async {
    try {
      final config = await rtcRouting(channel: channel, call: call);
      if (config['provider'] != 'cinnamon') return null;
      final choice = config['allow_region_selection'] == true ? region : null;
      final automatic = choice == 'automatic' ||
          (choice == null && config['default_region'] == null);
      return {
        'region': choice,
        'probe_ticket': config['probe_ticket'],
        'latency': automatic
            ? await measureRtcLatency((config['probes'] as List?) ?? [])
            : <String, int>{},
      };
    } on Object {
      return null;
    }
  }
}
