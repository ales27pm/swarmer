import { AppState } from "react-native";
import {
  createIPhoneCapabilityApiSession,
} from "@/lib/api/client";

import { IPhoneCapabilityBroker } from "./broker";
import { IPhoneCapabilityTransport } from "./transport";

export const iphoneCapabilityTransport = new IPhoneCapabilityTransport(
  new IPhoneCapabilityBroker(),
  {
    createSession: createIPhoneCapabilityApiSession,
    canExecute: () => AppState.currentState === "active",
  },
);
