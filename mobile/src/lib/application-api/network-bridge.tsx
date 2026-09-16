import { requireOptionalNativeModule } from "expo";
import { useEffect } from "react";
import { AppState } from "react-native";

import { applicationApi } from "@/lib/application-api";
import { createApplicationProtocol, type ProtocolRequest } from "@/lib/application-api/protocol";

type NativeRequest = ProtocolRequest & {
  requestId: string;
  foreground?: boolean;
  backgroundContinuation?: boolean;
};
type AutomationModule = {
  automationAvailable?: boolean;
  addListener(name: "automationRequest", listener: (request: NativeRequest) => void): { remove(): void };
  startAutomationServer(): Promise<{ enabled: boolean; port?: number; reason?: string }>;
  stopAutomationServer(): Promise<void>;
  reconcileAutomationServer?(): Promise<void>;
  completeAutomationRequest(requestId: string, status: number, body: string): Promise<void>;
};

// Preserve the idempotency ledger through React remounts for this JS process.
const protocol = createApplicationProtocol(applicationApi);

/** No network configuration or secrets cross the JavaScript bridge. */
export function ApplicationNetworkBridge() {
  useEffect(() => {
    const native = requireOptionalNativeModule<AutomationModule>("SwarmerLocalInference");
    // Embedded Expo bundles must use production JS. The native build, not Metro's
    // __DEV__ flag, decides whether the development-only transport can exist.
    if (native?.automationAvailable !== true || !native.startAutomationServer) return;
    let disposed = false;
    const subscription = native.addListener("automationRequest", (request) => {
      if (disposed) return;
      // These flags are supplied by the native listener, never by the HTTP body.
      // Native UIKit/admission state wins over a delayed React AppState event.
      if (typeof request.foreground === "boolean" && typeof request.backgroundContinuation === "boolean") {
        protocol.setAccess(request.foreground ? "foreground" : request.backgroundContinuation ? "continuation" : "inactive");
      } else {
        protocol.setActive(request.foreground === undefined && request.backgroundContinuation === undefined
          && AppState.currentState === "active");
      }
      let response;
      try {
        response = protocol.handle(request);
      } catch {
        response = { status: 400, body: { apiVersion: "1", error: { code: "invalid_request", message: "Requête invalide." } } };
      }
      void native.completeAutomationRequest(request.requestId, response.status, JSON.stringify(response.body)).catch(() => {
        // A lost connection is not a reason to replay the domain command.
      });
    });
    const activate = () => {
      const active = !disposed && AppState.currentState === "active";
      protocol.setActive(active);
      if (active) void native.startAutomationServer().catch(() => { /* native fail closed */ });
      else if (native.reconcileAutomationServer) void native.reconcileAutomationServer().catch(() => {
        void native.stopAutomationServer().catch(() => {});
      });
      else void native.stopAutomationServer().catch(() => {});
    };
    activate();
    const state = AppState.addEventListener("change", activate);
    return () => {
      disposed = true;
      protocol.setActive(false);
      subscription.remove();
      state.remove();
      void native.stopAutomationServer().catch(() => {});
    };
  }, []);
  return null;
}
