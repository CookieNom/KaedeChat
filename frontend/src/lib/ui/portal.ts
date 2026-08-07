/**
 * Move floating UI to the document root so transformed or scrollable
 * ancestors cannot clip viewport-positioned menus and popovers.
 *
 * Portal nodes must be removed when their owning Svelte block is destroyed.
 * Moving a node back into the component tree during teardown races Svelte's
 * own block cleanup and can leave a visible, detached "zombie" overlay whose
 * event handlers no longer work.
 */
export function portal(node: HTMLElement) {
  document.body.appendChild(node);

  return {
    destroy() {
      node.remove();
    }
  };
}
