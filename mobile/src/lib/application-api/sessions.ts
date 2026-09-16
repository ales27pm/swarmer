import { newGoalMessageId } from "@/lib/api/project";
import { subscribeConnectionChanges } from "@/lib/connection-events";
import { ApplicationApiError } from "./schema";

/** Private closures never cross the JSON boundary. Handles expire and cannot survive re-pairing. */
export class ApplicationSessions {
  private readonly entries = new Map<string, { kind: string; expiresAt: number; value: unknown }>();
  constructor(private readonly now = () => Date.now(), private readonly capacity = 32, private readonly ttlMs = 600_000) {}
  clear(): void { this.entries.clear(); }
  private prune(): void {
    for (const [id, entry] of this.entries) if (entry.expiresAt <= this.now()) this.entries.delete(id);
  }
  put<T>(kind: string, value: T): string {
    this.prune();
    if (this.entries.size >= this.capacity) throw new ApplicationApiError("session_limit", "Fermez ou laissez expirer les revues précédentes avant de poursuivre.");
    const id = `session_${newGoalMessageId()}`;
    this.entries.set(id, { kind, value, expiresAt: this.now() + this.ttlMs });
    return id;
  }
  get<T>(id: string, kind: string): T {
    this.prune();
    const entry = this.entries.get(id);
    if (!entry || entry.kind !== kind) throw new ApplicationApiError("session_expired", "Cette session n’est plus disponible. Rechargez la ressource sans rejouer une action incertaine.");
    return entry.value as T;
  }
}

export const applicationSessions = new ApplicationSessions();
subscribeConnectionChanges(() => applicationSessions.clear());
