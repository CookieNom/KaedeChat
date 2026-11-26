export type Entry = Record<string, string | boolean>;
export interface Pane {
  id: string;
  title: string;
  description: string;
  fields: { key: string; label: string; type?: string; placeholder?: string }[];
  records: Entry[];
  version: number;
  actions: [string, string][];
}
