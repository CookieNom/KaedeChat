import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';

final class KaedeApiClient {
  KaedeApiClient({required SessionVault vault, Dio? httpClient})
      : _vault = vault,
        _dio = httpClient ?? _defaultHttpClient() {
    _dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: _authorize,
        onError: _recover,
      ),
    );
  }

  final SessionVault _vault;
  final Dio _dio;
  static Dio _defaultHttpClient() => Dio(
        BaseOptions(
          connectTimeout: const Duration(seconds: 12),
          receiveTimeout: const Duration(seconds: 30),
          sendTimeout: const Duration(seconds: 30),
          headers: const <String, String>{
            'Accept': 'application/json',
            'X-Kaede-Client': 'mobile',
          },
        ),
      );
  final Dio uploadClient = Dio(
    BaseOptions(
      connectTimeout: const Duration(seconds: 12),
      sendTimeout: const Duration(minutes: 3),
      receiveTimeout: const Duration(seconds: 30),
      followRedirects: false,
    ),
  );
  SessionTokens? _tokens;
  Future<SessionTokens>? _refreshing;
  Domain? _selectedInstance;
  var _sessionGeneration = 0;
  final _sessionExpired = StreamController<void>.broadcast();
  static const _redirectStatuses = <int>{
    HttpStatus.movedPermanently,
    HttpStatus.found,
    HttpStatus.seeOther,
    HttpStatus.temporaryRedirect,
    HttpStatus.permanentRedirect,
  };

  /// Long-lived client for authenticated file transfers. Reuses the TLS
  /// connection across downloads and uploads instead of paying a fresh
  /// handshake for every media fetch.
  HttpClient? _fileClient;

  HttpClient _fileHttpClient() => _fileClient ??= HttpClient()
    ..connectionTimeout = const Duration(seconds: 12);

  void _closeFileClient() {
    _fileClient?.close(force: true);
    _fileClient = null;
  }

  SessionTokens? get tokens => _tokens;
  bool get signedIn => _tokens != null;
  Stream<void> get sessionExpired => _sessionExpired.stream;

  Future<String> installationId() => _vault.installationId();

  Future<RelayPushState?> relayPushState() => _vault.readRelayPushState();

  Future<void> saveRelayPushState(RelayPushState state) =>
      _vault.writeRelayPushState(state);

  Future<bool> pushOptedIn() => _vault.readPushOptIn();

  Future<bool?> pushOptInChoice() => _vault.readPushOptInChoice();

  Future<void> savePushOptIn(bool enabled) => _vault.writePushOptIn(enabled);

  Future<void> clearRelayPushState() => _vault.clearRelayPushState();

  Future<SessionTokens?> restore() async {
    _tokens = await _vault.read();
    _selectedInstance = _tokens?.instance;
    if (_selectedInstance case final instance?) {
      _dio.options.baseUrl = 'https://${instance.value}';
    }
    return _tokens;
  }

  void selectInstance(Domain instance) {
    if (_selectedInstance != instance) {
      _selectedInstance = instance;
      _sessionGeneration += 1;
    }
    _dio.options.baseUrl = 'https://${instance.value}';
  }

  Future<void> useTokens(SessionTokens tokens) async {
    if (_tokens?.accountKey != tokens.accountKey ||
        _tokens?.instance != tokens.instance) {
      _sessionGeneration += 1;
    }
    _tokens = tokens;
    _selectedInstance = tokens.instance;
    _dio.options.baseUrl = 'https://${tokens.instance.value}';
    await _vault.write(tokens);
  }

  Future<void> clearTokens({int? expectedGeneration}) async {
    if (expectedGeneration != null &&
        expectedGeneration != _sessionGeneration) {
      return;
    }
    _sessionGeneration += 1;
    _tokens = null;
    _closeFileClient();
    await _vault.clear();
  }

  Future<Map<String, Object?>> getJson(
    String path, {
    Map<String, Object?>? query,
  }) async {
    try {
      final response = await _dio.get<Object?>(path, queryParameters: query);
      return _jsonObject(response.data);
    } on DioException catch (error) {
      throw KaedeException.fromDio(error);
    }
  }

  Future<List<Map<String, Object?>>> getList(
    String path, {
    Map<String, Object?>? query,
  }) async {
    try {
      final response = await _dio.get<Object?>(path, queryParameters: query);
      return _jsonList(response.data);
    } on DioException catch (error) {
      throw KaedeException.fromDio(error);
    }
  }

  Future<Map<String, Object?>> postPublicJson(
    Uri uri, {
    required Map<String, Object?> data,
    required String expectedOrigin,
  }) async {
    if (uri.scheme != 'https' ||
        uri.host != expectedOrigin ||
        uri.userInfo.isNotEmpty ||
        uri.hasFragment) {
      throw const KaedeException(
        code: 'PUSH_RELAY_INVALID',
        message:
            'The configured notification relay is not trusted by this app.',
        status: 502,
      );
    }
    final client = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 12),
      receiveTimeout: const Duration(seconds: 20),
      sendTimeout: const Duration(seconds: 20),
      followRedirects: false,
      headers: const <String, String>{
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-Kaede-Client': 'mobile',
      },
    ));
    try {
      final response = await client.post<Object?>(uri.toString(), data: data);
      return _jsonObject(response.data);
    } on DioException catch (error) {
      throw KaedeException.fromDio(error);
    } finally {
      client.close(force: true);
    }
  }

  Future<void> deletePublic(
    Uri uri, {
    required String expectedOrigin,
    required Map<String, String> headers,
  }) async {
    if (uri.scheme != 'https' ||
        uri.host != expectedOrigin ||
        uri.userInfo.isNotEmpty ||
        uri.hasFragment) {
      throw const KaedeException(
        code: 'PUSH_RELAY_INVALID',
        message:
            'The configured notification relay is not trusted by this app.',
        status: 502,
      );
    }
    final client = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 12),
      receiveTimeout: const Duration(seconds: 20),
      sendTimeout: const Duration(seconds: 20),
      followRedirects: false,
      headers: <String, String>{
        'Accept': 'application/json',
        'X-Kaede-Client': 'mobile',
        ...headers,
      },
    ));
    try {
      await client.delete<Object?>(uri.toString());
    } on DioException catch (error) {
      throw KaedeException.fromDio(error);
    } finally {
      client.close(force: true);
    }
  }

  Future<List<int>> getBytes(String path) async {
    try {
      final response = await _dio.get<List<int>>(
        path,
        options: Options(responseType: ResponseType.bytes),
      );
      return response.data ?? const <int>[];
    } on DioException catch (error) {
      throw KaedeException.fromDio(error);
    }
  }

  Future<void> putPresigned(
    String url,
    List<int> bytes, {
    required String contentType,
  }) async {
    final uri = _safeExternalUri(url, purpose: 'upload');
    try {
      final response = await uploadClient.put<Object?>(
        uri.toString(),
        data: Stream<List<int>>.value(bytes),
        options: Options(
          headers: <String, Object>{
            Headers.contentTypeHeader: contentType,
            Headers.contentLengthHeader: bytes.length,
          },
          followRedirects: false,
          validateStatus: (status) =>
              status != null && status >= 200 && status < 300,
        ),
      );
      if ((response.statusCode ?? 500) >= 300) {
        throw const KaedeException(
            code: 'UPLOAD_FAILED',
            message: 'The upload could not be completed.',
            status: 502);
      }
    } on DioException catch (error) {
      final failure = KaedeException.fromDio(error);
      throw KaedeException(
        code: 'UPLOAD_FAILED',
        message: failure.status == 403
            ? 'The upload authorization expired. Choose the file and try again.'
            : failure.message,
        status: failure.status,
        traceId: failure.traceId,
        retryAfter: failure.retryAfter,
        details: failure.details,
      );
    }
  }

  Future<void> putPresignedFile(
    String url,
    File file, {
    required String contentType,
    void Function(int sent, int total)? onProgress,
  }) async {
    final uri = _safeExternalUri(url, purpose: 'upload');
    final client = _fileHttpClient();
    try {
      final request = await client.putUrl(uri);
      request.followRedirects = false;
      request.contentLength = await file.length();
      request.headers.contentType = ContentType.parse(contentType);
      var sent = 0;
      await for (final chunk in file.openRead()) {
        request.add(chunk);
        sent += chunk.length;
        onProgress?.call(sent, request.contentLength);
      }
      final response =
          await request.close().timeout(const Duration(minutes: 3));
      await response.drain<void>();
      if (response.statusCode < 200 || response.statusCode >= 300) {
        throw KaedeException(
          code: 'UPLOAD_FAILED',
          message: response.statusCode == 403
              ? 'The upload authorization expired. Choose the file and try again.'
              : response.statusCode >= 500
                  ? 'Attachment storage is temporarily unavailable. Try again later.'
                  : 'Attachment storage rejected the upload. Choose the file and try again.',
          status: response.statusCode,
        );
      }
    } on KaedeException {
      rethrow;
    } on Object catch (error) {
      throw KaedeException(
        code: 'UPLOAD_FAILED',
        message: error is FileSystemException
            ? 'Kaede could not read the selected file. Choose it again and check available device storage.'
            : error is TimeoutException
                ? 'The upload took too long. Check your connection and try again.'
                : 'Kaede could not reach attachment storage. Check your connection and try again.',
        status: 502,
      );
    }
  }

  static Uri _safeExternalUri(String value, {required String purpose}) {
    final uri = Uri.tryParse(value);
    if (uri == null ||
        uri.scheme != 'https' ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasFragment) {
      throw KaedeException(
        code: 'UNSAFE_${purpose.toUpperCase()}_URL',
        message: 'The server returned an unsafe $purpose address.',
        status: 502,
      );
    }
    return uri;
  }

  /// Streams authenticated media to [destination] without forwarding a Kaede
  /// bearer token if the API redirects to object storage on another origin.
  Future<File> downloadToFile(String path, File destination) async {
    final session = _tokens;
    final generation = _sessionGeneration;
    if (session == null) {
      throw const KaedeException(
        code: 'AUTHENTICATION_REQUIRED',
        message: 'Sign in to view this attachment.',
        status: 401,
      );
    }
    final client = _fileHttpClient();
    var uri = Uri.https(session.instance.value, path);
    try {
      for (var redirects = 0; redirects <= 5; redirects += 1) {
        if (_sessionGeneration != generation ||
            _tokens?.accountKey != session.accountKey) {
          throw const KaedeException(
            code: 'SESSION_CHANGED',
            message: 'The signed-in account changed during the download.',
            status: 409,
          );
        }
        final request = await client.getUrl(uri);
        request.followRedirects = false;
        request.headers.set(HttpHeaders.acceptHeader, '*/*');
        if (_origin(uri) == 'https://${session.instance.value}') {
          request.headers.set(
            HttpHeaders.authorizationHeader,
            'Bearer ${session.accessToken}',
          );
        }
        final response =
            await request.close().timeout(const Duration(seconds: 30));
        if (_redirectStatuses.contains(response.statusCode)) {
          final location = response.headers.value(HttpHeaders.locationHeader);
          await response.drain<void>();
          if (location == null || redirects == 5) {
            throw const KaedeException(
              code: 'MEDIA_DOWNLOAD_FAILED',
              message: 'The attachment could not be downloaded.',
              status: 502,
            );
          }
          final next = uri.resolve(location);
          if (next.scheme != 'https') {
            throw const KaedeException(
              code: 'UNSAFE_MEDIA_REDIRECT',
              message: 'The attachment used an unsafe redirect.',
              status: 502,
            );
          }
          uri = next;
          continue;
        }
        if (response.statusCode < 200 || response.statusCode >= 300) {
          final body = await _boundedErrorBody(response);
          final requestOptions = RequestOptions(path: uri.toString());
          final error = KaedeException.fromDio(DioException(
            requestOptions: requestOptions,
            type: DioExceptionType.badResponse,
            response: Response<Object?>(
              requestOptions: requestOptions,
              statusCode: response.statusCode,
              data: body,
              headers: Headers.fromMap(<String, List<String>>{
                if (response.headers.value(HttpHeaders.retryAfterHeader)
                    case final retry?)
                  HttpHeaders.retryAfterHeader: <String>[retry],
              }),
            ),
          ));
          throw error.code == 'HTTP_ERROR'
              ? KaedeException(
                  code: 'MEDIA_DOWNLOAD_FAILED',
                  message: response.statusCode == 403
                      ? 'You no longer have access to this attachment.'
                      : response.statusCode == 404
                          ? 'This attachment no longer exists.'
                          : response.statusCode >= 500
                              ? 'Attachment storage is temporarily unavailable. Try again later.'
                              : 'Attachment storage rejected the download. Try again.',
                  status: response.statusCode,
                  retryAfter: error.retryAfter,
                )
              : error;
        }
        final temporary = File('${destination.path}.part');
        await temporary.parent.create(recursive: true);
        final sink = temporary.openWrite();
        try {
          await response.pipe(sink);
        } on Object {
          await sink.close();
          if (await temporary.exists()) await temporary.delete();
          rethrow;
        }
        if (await destination.exists()) await destination.delete();
        return temporary.rename(destination.path);
      }
      throw const KaedeException(
        code: 'MEDIA_DOWNLOAD_FAILED',
        message: 'The attachment could not be downloaded.',
        status: 502,
      );
    } on KaedeException {
      rethrow;
    } on Object catch (error) {
      throw KaedeException(
        code: 'MEDIA_DOWNLOAD_FAILED',
        message: error is FileSystemException
            ? 'Kaede could not save this attachment. Check available device storage and try again.'
            : error is TimeoutException
                ? 'The attachment download took too long. Check your connection and try again.'
                : 'Kaede could not reach attachment storage. Check your connection and try again.',
        status: 502,
      );
    }
  }

  static Future<Object?> _boundedErrorBody(HttpClientResponse response) async {
    const maximum = 64 * 1024;
    final bytes = <int>[];
    await for (final chunk in response) {
      if (bytes.length + chunk.length > maximum) return null;
      bytes.addAll(chunk);
    }
    if (bytes.isEmpty) return null;
    try {
      return jsonDecode(utf8.decode(bytes));
    } on Object {
      return null;
    }
  }

  Future<Map<String, Object?>> sendJson(
    String method,
    String path, {
    Object? data,
    Map<String, Object?>? query,
    Map<String, String>? headers,
  }) async {
    try {
      final response = await _dio.request<Object?>(
        path,
        data: data,
        queryParameters: query,
        options: Options(method: method, headers: headers),
      );
      if (response.data == null ||
          response.statusCode == HttpStatus.noContent) {
        return const <String, Object?>{};
      }
      return _jsonObject(response.data);
    } on DioException catch (error) {
      throw KaedeException.fromDio(error);
    }
  }

  Future<List<Map<String, Object?>>> sendJsonList(
    String method,
    String path, {
    Object? data,
    Map<String, Object?>? query,
    Map<String, String>? headers,
  }) async {
    try {
      final response = await _dio.request<Object?>(
        path,
        data: data,
        queryParameters: query,
        options: Options(method: method, headers: headers),
      );
      if (response.data == null ||
          response.statusCode == HttpStatus.noContent) {
        return const <Map<String, Object?>>[];
      }
      return _jsonList(response.data);
    } on DioException catch (error) {
      throw KaedeException.fromDio(error);
    }
  }

  Future<void> _authorize(
      RequestOptions options, RequestInterceptorHandler handler) async {
    final requestOrigin = _origin(options.uri);
    options.extra['kaedeSessionGeneration'] = _sessionGeneration;
    options.extra['kaedeRequestOrigin'] = requestOrigin;
    if (_tokens case final tokens?
        when shouldAttachKaedeAuthorization(options.uri, tokens.instance)) {
      options.headers['Authorization'] = 'Bearer ${tokens.accessToken}';
    } else {
      options.headers.remove('Authorization');
    }
    handler.next(options);
  }

  Future<void> _recover(
      DioException error, ErrorInterceptorHandler handler) async {
    final request = error.requestOptions;
    final requestGeneration = request.extra['kaedeSessionGeneration'] as int?;
    final requestOrigin = request.extra['kaedeRequestOrigin'] as String?;
    final current = _tokens;
    if (error.response?.statusCode != 401 ||
        request.extra['kaedeRetried'] == true ||
        request.path.endsWith('/auth/refresh') ||
        current == null ||
        requestGeneration != _sessionGeneration ||
        requestOrigin != 'https://${current.instance.value}' ||
        !isKaedeApiPath(request.uri.path)) {
      handler.next(error);
      return;
    }
    try {
      final tokens = await _refreshSingleFlight(requestGeneration!);
      if (requestGeneration != _sessionGeneration ||
          requestOrigin != 'https://${tokens.instance.value}') {
        handler.next(error);
        return;
      }
      request.extra['kaedeRetried'] = true;
      request.headers['Authorization'] = 'Bearer ${tokens.accessToken}';
      handler.resolve(await _dio.fetch<Object?>(request));
    } on Object {
      handler.next(error);
    }
  }

  Future<SessionTokens> _refreshSingleFlight(int generation) {
    if (_refreshing case final existing?) return existing;
    final created = _refresh(generation);
    _refreshing = created;
    created.whenComplete(() {
      if (identical(_refreshing, created)) _refreshing = null;
    });
    return created;
  }

  Future<SessionTokens> _refresh(int generation) async {
    final current = _tokens;
    if (current == null || generation != _sessionGeneration) {
      throw const KaedeException(
        code: 'SESSION_CHANGED',
        message: 'The active session changed.',
        status: 401,
      );
    }
    final refreshClient = Dio(
      BaseOptions(
        connectTimeout: const Duration(seconds: 12),
        receiveTimeout: const Duration(seconds: 20),
        sendTimeout: const Duration(seconds: 20),
        followRedirects: false,
        validateStatus: (status) => status != null && status < 500,
        headers: const <String, String>{
          'Accept': 'application/json',
          'X-Kaede-Client': 'mobile',
        },
      ),
    );
    try {
      final response = await refreshClient.post<Map<String, Object?>>(
        'https://${current.instance.value}/api/v1/auth/refresh',
        data: <String, Object?>{'refresh_token': current.refreshToken},
      );
      if (response.statusCode case 400 || 401) {
        await clearTokens(expectedGeneration: generation);
        _sessionExpired.add(null);
        throw const KaedeException(
          code: 'SESSION_EXPIRED',
          message: 'Your session expired. Sign in again.',
          status: 401,
        );
      }
      if ((response.statusCode ?? 500) >= 300) {
        throw KaedeException(
          code: 'REFRESH_UNAVAILABLE',
          message: 'The session could not be refreshed right now.',
          status: response.statusCode ?? 503,
        );
      }
      final body = response.data;
      final access = body?['access_token'];
      final refresh = body?['refresh_token'];
      if (access is! String || refresh is! String) {
        throw const FormatException('Invalid refresh response');
      }
      if (generation != _sessionGeneration ||
          _tokens?.refreshToken != current.refreshToken) {
        throw const KaedeException(
          code: 'SESSION_CHANGED',
          message: 'The active session changed.',
          status: 401,
        );
      }
      final updated = current.copyWith(
        accessToken: access,
        refreshToken: refresh,
      );
      _tokens = updated;
      await _vault.write(updated);
      return updated;
    } on DioException catch (error) {
      throw KaedeException.fromDio(error);
    }
  }
}

String _origin(Uri uri) {
  final port = uri.hasPort && uri.port != 443 ? ':${uri.port}' : '';
  return '${uri.scheme.toLowerCase()}://${uri.host.toLowerCase()}$port';
}

bool isKaedeApiPath(String path) =>
    path == '/api/v1' || path.startsWith('/api/v1/');

bool shouldAttachKaedeAuthorization(Uri uri, Domain instance) =>
    _origin(uri) == 'https://${instance.value}' && isKaedeApiPath(uri.path);

Map<String, Object?> _jsonObject(Object? value) {
  if (value case final Map<Object?, Object?> map) {
    return map.map((key, item) => MapEntry('$key', item));
  }
  throw const FormatException('Expected a JSON object');
}

List<Map<String, Object?>> _jsonList(Object? value) {
  if (value is! List) throw const FormatException('Expected a JSON array');
  return value
      .whereType<Map<Object?, Object?>>()
      .map((item) => item.map((key, value) => MapEntry('$key', value)))
      .toList();
}
