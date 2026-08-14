export const prerender = false;

export const load = ({ params }: { params: { applicationRef: string; templateSlug: string } }) => ({
  applicationRef: decodeURIComponent(params.applicationRef),
  templateSlug: params.templateSlug
});
