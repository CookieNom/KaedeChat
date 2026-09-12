export function isAttachmentSpoiler(filename: string): boolean {
  return filename.startsWith('SPOILER_');
}

export function spoilerFilename(filename: string, spoiler: boolean): string {
  const name = filename.replace(/^(SPOILER_)+/, '') || 'file';
  if (!spoiler) return name;
  const extension = name.includes('.') ? name.slice(name.lastIndexOf('.')) : '';
  const suffix = extension.length <= 16 ? extension : '';
  return 'SPOILER_' + (name.length > 247 ? name.slice(0, 247 - suffix.length) + suffix : name);
}
