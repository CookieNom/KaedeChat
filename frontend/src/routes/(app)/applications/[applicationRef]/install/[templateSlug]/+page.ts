import { safeDirectoryApplicationReturnPath } from '$lib/chat/application-directory';

export const prerender = false;

export const load = ({
  params,
  url
}: {
  params: { applicationRef: string; templateSlug: string };
  url: URL;
}) => ({
  applicationRef: params.applicationRef,
  templateSlug: params.templateSlug,
  returnTo:
    safeDirectoryApplicationReturnPath(url.searchParams.get('return_to'), url.origin) ?? '/home'
});
