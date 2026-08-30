import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/developer_portal.dart';

extension DeveloperPortalRepository on KaedeRepository {
  Future<List<DeveloperTeam>> developerTeams() async =>
      (await api.getList('/api/v1/developer-teams'))
          .map(DeveloperTeam.fromJson)
          .toList(growable: false);

  Future<DeveloperTeam> createDeveloperTeam(String name) async =>
      DeveloperTeam.fromJson(await api.sendJson(
        'POST',
        '/api/v1/developer-teams',
        data: <String, Object?>{'name': name.trim()},
      ));

  Future<List<DeveloperTeamMember>> developerTeamMembers(
    EntityRef team,
  ) async =>
      (await api.getList(
        '/api/v1/developer-teams/${team.pathSegment}/members',
      ))
          .map(DeveloperTeamMember.fromJson)
          .toList(growable: false);

  Future<DeveloperTeamMember> addDeveloperTeamMember({
    required EntityRef team,
    required EntityRef user,
    required String role,
  }) async =>
      DeveloperTeamMember.fromJson(await api.sendJson(
        'POST',
        '/api/v1/developer-teams/${team.pathSegment}/members',
        data: <String, Object?>{'user_ref': user.wire, 'role': role},
      ));

  Future<DeveloperTeamMember> updateDeveloperTeamMember({
    required EntityRef team,
    required EntityRef user,
    required String role,
  }) async =>
      DeveloperTeamMember.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/developer-teams/${team.pathSegment}/members/${user.pathSegment}',
        data: <String, Object?>{'role': role},
      ));

  Future<void> removeDeveloperTeamMember({
    required EntityRef team,
    required EntityRef user,
  }) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/developer-teams/${team.pathSegment}/members/${user.pathSegment}',
    );
  }

  Future<DeveloperApplicationDetail> createDeveloperApplication({
    required String name,
    EntityRef? team,
    String? description,
  }) async =>
      DeveloperApplicationDetail.fromJson(await api.sendJson(
        'POST',
        '/api/v1/applications',
        data: <String, Object?>{
          'name': name.trim(),
          'description':
              description?.trim().isEmpty == true ? null : description?.trim(),
          if (team != null) 'team_ref': team.wire,
        },
      ));

  Future<DeveloperApplicationDetail> developerApplication(
    EntityRef application,
  ) async =>
      DeveloperApplicationDetail.fromJson(await api.getJson(
        '/api/v1/applications/${application.pathSegment}',
      ));

  Future<DeveloperApplicationDetail> updateDeveloperApplication(
    EntityRef application,
    Map<String, Object?> patch,
  ) async =>
      DeveloperApplicationDetail.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/applications/${application.pathSegment}',
        data: patch,
      ));

  Future<List<Map<String, Object?>>> applicationCommands(
    EntityRef application,
  ) =>
      api.getList('/api/v1/applications/${application.pathSegment}/commands');

  Future<void> replaceApplicationCommands(
    EntityRef application,
    List<Object?> commands,
  ) async {
    await api.sendJson(
      'PUT',
      '/api/v1/applications/${application.pathSegment}/commands',
      data: <String, Object?>{'commands': commands},
    );
  }

  Future<List<DeveloperCredential>> applicationCredentials(
    EntityRef application,
  ) async =>
      (await api.getList(
        '/api/v1/applications/${application.pathSegment}/credentials',
      ))
          .map(DeveloperCredential.fromJson)
          .toList(growable: false);

  Future<String> createApplicationCredential(
    EntityRef application,
    String label,
  ) async {
    final result = await api.sendJson(
      'POST',
      '/api/v1/applications/${application.pathSegment}/credentials',
      data: <String, Object?>{
        'label': label.trim(),
        'scopes': const <String>['workers.manage', 'commands.manage'],
      },
    );
    final token = result['token'];
    if (token is! String || token.isEmpty) {
      throw const FormatException(
          'The server did not return the new credential.');
    }
    return token;
  }

  Future<void> revokeApplicationCredential(
    EntityRef application,
    String credentialId,
  ) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/applications/${application.pathSegment}/credentials/${Uri.encodeComponent(credentialId)}',
    );
  }

  Future<List<DeveloperWorker>> applicationWorkers(
    EntityRef application,
  ) async =>
      (await api.getList(
        '/api/v1/applications/${application.pathSegment}/workers',
      ))
          .map(DeveloperWorker.fromJson)
          .toList(growable: false);

  Future<void> createApplicationWorker({
    required EntityRef application,
    required String name,
    required String publicKey,
    required List<String> scopes,
    required List<String> intents,
    required List<String> targetDomains,
    int sessionLimit = 1,
  }) async {
    await api.sendJson(
      'POST',
      '/api/v1/applications/${application.pathSegment}/workers',
      data: <String, Object?>{
        'name': name.trim(),
        'public_key': publicKey.trim(),
        'scopes': scopes,
        'intents': intents,
        'target_domains': targetDomains,
        'session_limit': sessionLimit,
      },
    );
  }

  Future<void> revokeApplicationWorker(
    EntityRef application,
    String workerId,
  ) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/applications/${application.pathSegment}/workers/${Uri.encodeComponent(workerId)}',
    );
  }

  Future<List<DeveloperInstallTemplate>> applicationInstallTemplates(
    EntityRef application,
  ) async =>
      (await api.getList(
        '/api/v1/applications/${application.pathSegment}/install-templates',
      ))
          .map(DeveloperInstallTemplate.fromJson)
          .toList(growable: false);

  Future<void> createApplicationInstallTemplate({
    required EntityRef application,
    required String slug,
    required String name,
    String? description,
    required List<String> scopes,
    required List<String> intents,
    required String permissions,
    required String e2eeMode,
  }) async {
    await api.sendJson(
      'POST',
      '/api/v1/applications/${application.pathSegment}/install-templates',
      data: <String, Object?>{
        'slug': slug.trim(),
        'name': name.trim(),
        'description':
            description?.trim().isEmpty == true ? null : description?.trim(),
        'scopes': scopes,
        'intents': intents,
        'permissions': permissions,
        'contexts': const <String>['guild'],
        'e2ee_mode': e2eeMode,
      },
    );
  }

  Future<List<DeveloperInstallation>> applicationInstallations(
    EntityRef application,
  ) async =>
      (await api.getList(
        '/api/v1/applications/${application.pathSegment}/installations',
      ))
          .map(DeveloperInstallation.fromJson)
          .toList(growable: false);

  Future<List<DeveloperInstanceRule>> applicationInstanceRules(
    EntityRef application,
  ) async =>
      (await api.getList(
        '/api/v1/applications/${application.pathSegment}/instance-rules',
      ))
          .map(DeveloperInstanceRule.fromJson)
          .toList(growable: false);

  Future<void> putApplicationInstanceRule({
    required EntityRef application,
    required String targetDomain,
    required String effect,
  }) async {
    await api.sendJson(
      'PUT',
      '/api/v1/applications/${application.pathSegment}/instance-rules/${Uri.encodeComponent(targetDomain.trim())}',
      data: <String, Object?>{'effect': effect},
    );
  }

  Future<void> deleteApplicationInstanceRule({
    required EntityRef application,
    required String targetDomain,
  }) async {
    await api.sendJson(
      'DELETE',
      '/api/v1/applications/${application.pathSegment}/instance-rules/${Uri.encodeComponent(targetDomain)}',
    );
  }
}
