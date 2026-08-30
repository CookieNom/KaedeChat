import type { PageLoad } from './$types';
import { safeDirectoryListReturnPath } from '$lib/chat/application-directory';

export const prerender = false;

export const load: PageLoad = ({ params, url }) => ({
  applicationRef: params.applicationRef,
  returnTo:
    safeDirectoryListReturnPath(url.searchParams.get('return_to'), url.origin) ??
    '/application-directory'
});
