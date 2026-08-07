/**
 * Move floating UI to the document root so transformed or scrollable
 * ancestors cannot clip viewport-positioned menus and popovers.
 */
export function portal(node: HTMLElement) {
  const parent = node.parentNode;
  const anchor = document.createComment('portal');
  parent?.insertBefore(anchor, node);
  document.body.appendChild(node);

  return {
    destroy() {
      if (anchor.parentNode) {
        anchor.parentNode.insertBefore(node, anchor);
        anchor.remove();
      } else {
        node.remove();
      }
    }
  };
}
