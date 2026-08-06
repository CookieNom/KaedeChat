export interface DispatchBatch<T> {
  readonly items: T[];
}

export class DispatchReplayBuffer<T> {
  #active: DispatchBatch<T> | null = null;

  begin(): DispatchBatch<T> {
    const batch = { items: this.#active ? [...this.#active.items] : [] };
    this.#active = batch;
    return batch;
  }

  push(item: T): boolean {
    if (!this.#active) return false;
    this.#active.items.push(item);
    return true;
  }

  finish(batch: DispatchBatch<T>): T[] | null {
    if (batch !== this.#active) return null;
    this.#active = null;
    return batch.items.splice(0);
  }

  clear(): void {
    this.#active = null;
  }
}
