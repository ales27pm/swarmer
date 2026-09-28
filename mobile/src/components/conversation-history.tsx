import { Modal, Pressable, ScrollView, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { COLORS, timeAgo } from "@/components/swarm-ui";
import type { Conversation } from "@/lib/api/types";

type Props = {
  conversations: Conversation[]; selectedId?: string; disabled: boolean;
  connected: boolean; onSelect: (id: string) => void; onNew: () => void;
};

export function ConversationHistory({ conversations, selectedId, disabled, connected, onSelect, onNew }: Props) {
  return <View style={{ gap: 16 }} testID="conversation-history">
    <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 20, fontWeight: "700" }}>Historique</Text>
    <Pressable accessibilityRole="button" disabled={disabled} accessibilityState={{ disabled }} onPress={onNew}
      style={{ minHeight: 44, justifyContent: "center", opacity: disabled ? 0.5 : 1 }}>
      <Text style={{ color: COLORS.accent, fontWeight: "600" }}>Nouvelle discussion</Text>
    </Pressable>
    {!connected ? <Text style={{ color: COLORS.muted, lineHeight: 20 }}>Connecte le serveur pour retrouver tes discussions.</Text>
      : !conversations.length ? <Text style={{ color: COLORS.muted, lineHeight: 20 }}>Ta première discussion apparaîtra ici après l’envoi d’un message.</Text> : null}
    {conversations.map((conversation) => <Pressable key={conversation.id} accessibilityRole="button"
      accessibilityLabel={`Reprendre ${conversation.title}`} accessibilityState={{ selected: conversation.id === selectedId, disabled }}
      disabled={disabled} onPress={() => onSelect(conversation.id)}
      style={({ pressed }) => ({ gap: 6, padding: 12, minHeight: 64, borderRadius: 12,
        backgroundColor: selectedId === conversation.id ? COLORS.panelRaised : "transparent", opacity: disabled ? 0.5 : pressed ? 0.7 : 1 })}>
      <Text numberOfLines={2} style={{ color: COLORS.text, fontSize: 15, fontWeight: "600", lineHeight: 21 }}>{conversation.title}</Text>
      {conversation.last_message ? <Text numberOfLines={2} style={{ color: COLORS.muted, fontSize: 13, lineHeight: 19 }}>{conversation.last_message}</Text> : null}
      <Text style={{ color: COLORS.subtle, fontSize: 12 }}>{timeAgo(conversation.updated_at)}</Text>
    </Pressable>)}
    {conversations.length >= 50 ? <Text style={{ color: COLORS.subtle, fontSize: 12 }}>Les 50 discussions les plus récentes sont affichées.</Text> : null}
  </View>;
}

export function HistoryDrawer({ visible, onClose, ...props }: Props & { visible: boolean; onClose: () => void }) {
  const insets = useSafeAreaInsets();
  return <Modal transparent visible={visible} animationType="slide" onRequestClose={onClose}>
    <View style={{ flex: 1, backgroundColor: "#0009", justifyContent: "flex-end" }}>
      <Pressable accessibilityRole="button" accessibilityLabel="Fermer l’historique" onPress={onClose} style={{ flex: 1, minHeight: 44 }} />
      <View accessibilityViewIsModal style={{ maxHeight: "85%", backgroundColor: COLORS.panel, borderTopLeftRadius: 24, borderTopRightRadius: 24, padding: 20, paddingBottom: Math.max(insets.bottom, 20), gap: 8 }}>
        <Pressable accessibilityRole="button" onPress={onClose} style={{ alignSelf: "flex-end", minHeight: 44, justifyContent: "center" }}>
          <Text style={{ color: COLORS.accent, fontWeight: "600" }}>Fermer</Text>
        </Pressable>
        <ScrollView><ConversationHistory {...props} /></ScrollView>
      </View>
    </View>
  </Modal>;
}
