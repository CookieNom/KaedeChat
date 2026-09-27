import { compactBadgeCount } from '$lib/notifications/counts';
import { nativeInvoke } from './native';

export async function setTaskbarUnreadCount(count: number): Promise<void> {
  let rgba: number[] = [];
  if (count > 0) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 32;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Could not draw the taskbar badge');
    context.fillStyle = '#ed4245';
    context.beginPath();
    context.arc(16, 16, 16, 0, Math.PI * 2);
    context.fill();
    context.fillStyle = '#ffffff';
    context.font = `bold ${count > 99 ? 15 : count > 9 ? 20 : 24}px sans-serif`;
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillText(compactBadgeCount(count), 16, 17);
    rgba = Array.from(context.getImageData(0, 0, 32, 32).data);
  }
  await nativeInvoke('native_set_unread_badge', { count, rgba });
}
