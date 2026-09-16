import type { IPhoneCapabilityTransport } from "@/lib/iphone-capabilities/transport";
import { invokeApplicationCommand } from "./registry";

/** Both callers share the existing singleton's consumed-grant and replay protection. */
export const iphoneCapabilityTransport: Pick<IPhoneCapabilityTransport, "refresh" | "load" | "authorize" | "execute"> = {
  refresh: () => invokeApplicationCommand("iphone.requests.list", {}),
  load: (id) => invokeApplicationCommand("iphone.requests.get", { id }),
  authorize: (id, decision) => invokeApplicationCommand("iphone.requests.decide", { id, decision, confirm: true }),
  execute: (id) => invokeApplicationCommand("iphone.requests.execute", { id, confirm: true }),
};
