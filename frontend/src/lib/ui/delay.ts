export function cancelableDelay(
  milliseconds: number,
  signal?: AbortSignal,
  message = 'Operation cancelled'
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException(message, 'AbortError'));
      return;
    }
    const timeout = setTimeout(finish, milliseconds);
    function finish() {
      signal?.removeEventListener('abort', cancel);
      resolve();
    }
    function cancel() {
      clearTimeout(timeout);
      signal?.removeEventListener('abort', cancel);
      reject(new DOMException(message, 'AbortError'));
    }
    signal?.addEventListener('abort', cancel, { once: true });
  });
}
