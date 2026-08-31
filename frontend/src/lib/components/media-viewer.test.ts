import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const viewer = readFileSync(new URL('./MediaViewer.svelte', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../../styles.css', import.meta.url), 'utf8');

describe('media viewer zoom', () => {
  it('fits images by default and exposes mouse and keyboard zoom controls', () => {
    expect(viewer).toContain('const ZOOM_LEVELS = [1, 1.25, 1.5, 2, 3, 4]');
    expect(viewer).toContain('onwheel={zoomWheel}');
    expect(viewer).toContain("event.key === '0'");
    expect(viewer).toContain('aria-label="Zoom in"');
    expect(viewer).toContain("zoomIndex === 0 ? 'Fit'");
    expect(styles).toContain('.media-viewer-image-canvas img');
    expect(styles).toMatch(
      /\.media-viewer-image-canvas img \{[^}]*width: 100%;[^}]*height: 100%;/su
    );
  });
});
