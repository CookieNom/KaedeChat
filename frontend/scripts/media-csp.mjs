const LOCAL_HTTP_HOST = /^(?:localhost|127(?:\.\d{1,3}){3}|\[::1\]|(?:[a-z0-9-]+\.)+localhost)$/i;
const BUCKET_NAME = /^[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?$/;

function exactUploadOrigin(configured, settingName) {
  const target = new URL(configured);
  if (
    target.username ||
    target.password ||
    target.search ||
    target.hash ||
    (target.pathname !== '' && target.pathname !== '/')
  ) {
    throw new Error(`${settingName} values must be origins without credentials or paths`);
  }
  if (
    target.protocol !== 'https:' &&
    !(target.protocol === 'http:' && LOCAL_HTTP_HOST.test(target.hostname))
  ) {
    throw new Error('browser media origins must use HTTPS outside local development');
  }
  return target;
}

/** Return the exact browser origins used for attachment-bucket PUT requests. */
export function mediaUploadConnectSources(environment = process.env) {
  const explicitOrigins = environment.KAEDE_MEDIA_UPLOAD_ORIGINS?.trim();
  if (explicitOrigins) {
    return [
      ...new Set(
        explicitOrigins
          .split(/[\s,]+/u)
          .filter(Boolean)
          .map((origin) => exactUploadOrigin(origin, 'KAEDE_MEDIA_UPLOAD_ORIGINS').origin)
      )
    ];
  }

  const configured = environment.KAEDE_MEDIA_PUBLIC_BASE_URL?.trim();
  if (!configured) return [];

  const target = exactUploadOrigin(configured, 'KAEDE_MEDIA_PUBLIC_BASE_URL');

  const addressingStyle = environment.KAEDE_MEDIA_S3_ADDRESSING_STYLE?.trim() || 'path';
  if (addressingStyle === 'path') return [target.origin];
  if (addressingStyle !== 'virtual') {
    throw new Error('KAEDE_MEDIA_S3_ADDRESSING_STYLE must be path or virtual');
  }

  const bucket = environment.KAEDE_MEDIA_ATTACHMENTS_BUCKET?.trim() || 'kaede-attachments';
  if (!BUCKET_NAME.test(bucket)) throw new Error('invalid attachment bucket for CSP generation');
  target.hostname = `${bucket}.${target.hostname}`;
  return [target.origin];
}
