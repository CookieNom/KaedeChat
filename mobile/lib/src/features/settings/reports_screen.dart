import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

final class MyReportsScreen extends ConsumerStatefulWidget {
  const MyReportsScreen({super.key});

  @override
  ConsumerState<MyReportsScreen> createState() => _MyReportsScreenState();
}

final class _MyReportsScreenState extends ConsumerState<MyReportsScreen> {
  List<Map<String, Object?>> _reports = const [];
  Object? _error;
  var _loading = true;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final reports = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .myReports();
      if (mounted) {
        setState(() {
          _reports = reports;
          _error = null;
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() {
          _error = error;
          _loading = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: Text('My reports')),
        body: RefreshIndicator(
          onRefresh: _load,
          child: ListView(
            padding: EdgeInsets.fromLTRB(16, 12, 16, 32),
            children: [
              Text(
                'Reports go to your home instance’s Trust & Safety team, not guild moderators.',
                style: TextStyle(color: context.kaede.muted, height: 1.4),
              ),
              SizedBox(height: 16),
              if (_loading)
                Center(child: CircularProgressIndicator())
              else if (_error case final error?)
                _ReportsNotice(
                  text: userFacingError(error,
                      summary: 'Could not load your reports'),
                  onRetry: _load,
                )
              else if (_reports.isEmpty)
                const _ReportsEmpty()
              else
                for (final report in _reports) _ReportCard(report),
            ],
          ),
        ),
      );
}

final class _ReportCard extends StatelessWidget {
  const _ReportCard(this.report);
  final Map<String, Object?> report;

  @override
  Widget build(BuildContext context) {
    final created =
        DateTime.tryParse('${report['created_at'] ?? ''}')?.toLocal();
    return Card(
      margin: EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Expanded(
                child: Text(
                  '${report['category'] ?? 'report'}'.replaceAll('_', ' '),
                  style: TextStyle(fontWeight: FontWeight.w800),
                ),
              ),
              _StatusPill('${report['status'] ?? 'submitted'}'),
            ]),
            SizedBox(height: 6),
            Text(
                '${report['target_type'] ?? 'item'} · ${report['target_ref'] ?? ''}',
                style: TextStyle(color: context.kaede.muted, fontSize: 12)),
            if ('${report['description'] ?? ''}'.trim().isNotEmpty) ...[
              SizedBox(height: 10),
              Text('${report['description']}',
                  style: TextStyle(color: context.kaede.textSoft)),
            ],
            if (created != null) ...[
              SizedBox(height: 10),
              Text('Submitted ${DateFormat.yMMMd().add_jm().format(created)}',
                  style: TextStyle(color: context.kaede.muted, fontSize: 11)),
            ],
          ],
        ),
      ),
    );
  }
}

final class _StatusPill extends StatelessWidget {
  const _StatusPill(this.status);
  final String status;
  @override
  Widget build(BuildContext context) => Container(
        padding: EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(99),
        ),
        child: Text(status.replaceAll('_', ' '),
            style: TextStyle(color: context.kaede.muted, fontSize: 11)),
      );
}

final class _ReportsNotice extends StatelessWidget {
  const _ReportsNotice({required this.text, required this.onRetry});
  final String text;
  final Future<void> Function() onRetry;
  @override
  Widget build(BuildContext context) => Card(
        child: ListTile(
          leading: Icon(Icons.error_outline_rounded),
          title: Text(text),
          trailing: TextButton(onPressed: onRetry, child: Text('Retry')),
        ),
      );
}

final class _ReportsEmpty extends StatelessWidget {
  const _ReportsEmpty();
  @override
  Widget build(BuildContext context) => Card(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Column(children: [
            Icon(Icons.flag_outlined, size: 34, color: context.kaede.muted),
            SizedBox(height: 10),
            Text('No reports', style: TextStyle(fontWeight: FontWeight.w800)),
            SizedBox(height: 4),
            Text('Reports submitted from a message menu will appear here.',
                textAlign: TextAlign.center,
                style: TextStyle(color: context.kaede.muted)),
          ]),
        ),
      );
}
