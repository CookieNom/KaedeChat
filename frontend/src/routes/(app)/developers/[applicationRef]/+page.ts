export const prerender = false;

export const load = ({ params }: { params: { applicationRef: string } }) => ({
  applicationRef: decodeURIComponent(params.applicationRef)
});
