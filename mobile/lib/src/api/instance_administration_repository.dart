import 'dart:io';

import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/instance_administration.dart';

extension InstanceAdministrationRepository on KaedeRepository {
  Future<AdministrationIdentity> administrationIdentity() async =>
      AdministrationIdentity.fromJson(
        await api.getJson('/api/v1/administration/@me'),
      );

  Future<Map<String, int>> administrationOverview() async {
    final json = await api.getJson('/api/v1/administration/overview');
    return json.map(
      (key, value) => MapEntry(key, (value as num?)?.toInt() ?? 0),
    );
  }

  Future<List<AdministrationUser>> administrationUsers({
    String? query,
  }) async =>
      (await api.getList(
        '/api/v1/administration/users',
        query: query?.trim().isNotEmpty == true
            ? <String, Object?>{'query': query!.trim()}
            : null,
      ))
          .map(AdministrationUser.fromJson)
          .toList(growable: false);

  Future<AdministrationUser> updateAdministrationUser(
    EntityRef user,
    Map<String, Object?> patch,
  ) async =>
      AdministrationUser.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/administration/users/${user.pathSegment}',
        data: patch,
      ));

  Future<List<AdministrationApplication>> administrationApplications() async =>
      (await api.getList('/api/v1/administration/applications'))
          .map(AdministrationApplication.fromJson)
          .toList(growable: false);

  Future<void> updateAdministrationApplication(
    EntityRef application, {
    required String status,
    String? reason,
  }) async {
    await api.sendJson(
      'PATCH',
      '/api/v1/administration/applications/${application.pathSegment}',
      data: <String, Object?>{'status': status, 'reason': reason},
    );
  }

  Future<List<AdministrationReport>> administrationReports() async =>
      (await api.getList('/api/v1/administration/reports'))
          .map(AdministrationReport.fromJson)
          .toList(growable: false);

  Future<AdministrationReport> updateAdministrationReport({
    required String reportId,
    required String status,
    String? resolution,
  }) async =>
      AdministrationReport.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/administration/reports/${Uri.encodeComponent(reportId)}',
        data: <String, Object?>{
          'status': status,
          'resolution':
              resolution?.trim().isEmpty == true ? null : resolution?.trim(),
        },
      ));

  Future<File> downloadAdministrationReportAttachment({
    required AdministrationReport report,
    required AdministrationReportAttachment attachment,
    required File destination,
    String variant = 'original',
  }) {
    if (!const <String>{
      'original',
      'thumbnail_128',
      'thumbnail_512',
      'thumbnail_1024',
      'poster',
    }.contains(variant)) {
      throw ArgumentError.value(
          variant, 'variant', 'Unsupported media variant');
    }
    return api.downloadToFile(
      '/api/v1/administration/reports/${Uri.encodeComponent(report.id)}/attachment/$variant',
      destination,
      query: <String, Object?>{'attachment_ref': attachment.ref},
    );
  }

  Future<AdministrationReport> enforceAdministrationReport({
    required String reportId,
    required String accountAction,
    required String messageAction,
    required String reason,
  }) async {
    final result = await api.sendJson(
      'POST',
      '/api/v1/administration/reports/${Uri.encodeComponent(reportId)}/actions',
      data: <String, Object?>{
        'account_action': accountAction,
        'message_action': messageAction,
        'reason': reason.trim(),
      },
    );
    return AdministrationReport.fromJson(
      Map<String, Object?>.from(result['report']! as Map),
    );
  }

  Future<List<AdministrationInstanceBlock>> administrationBlocks() async =>
      (await api.getList('/api/v1/administration/instances/blocks'))
          .map(AdministrationInstanceBlock.fromJson)
          .toList(growable: false);

  Future<void> putAdministrationBlock({
    required String domain,
    required String level,
    required bool includeSubdomains,
    String? reason,
  }) async {
    await api.sendJson(
      'PUT',
      '/api/v1/administration/instances/blocks',
      data: <String, Object?>{
        'domain': domain.trim(),
        'level': level,
        'include_subdomains': includeSubdomains,
        'reason': reason?.trim().isEmpty == true ? null : reason?.trim(),
      },
    );
  }

  Future<void> deleteAdministrationBlock(String domain) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/administration/instances/blocks/${Uri.encodeComponent(domain)}',
    );
  }

  Future<List<AdministrationOperator>> administrationOperators() async =>
      (await api.getList('/api/v1/administration/operators'))
          .map(AdministrationOperator.fromJson)
          .toList(growable: false);

  Future<void> addAdministrationOperator({
    required EntityRef user,
    required String role,
  }) async {
    await api.sendJson(
      'POST',
      '/api/v1/administration/operators',
      data: <String, Object?>{'user_ref': user.wire, 'role': role},
    );
  }

  Future<void> removeAdministrationOperator(String grantId) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/administration/operators/${Uri.encodeComponent(grantId)}',
    );
  }

  Future<List<AdministrationAuditEvent>> administrationAudit() async =>
      (await api.getList('/api/v1/administration/audit'))
          .map(AdministrationAuditEvent.fromJson)
          .toList(growable: false);
}
