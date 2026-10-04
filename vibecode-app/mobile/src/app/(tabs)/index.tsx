import React, { useState } from "react";
import { FlatList, KeyboardAvoidingView, Platform, Pressable, Text, TextInput, View } from "react-native";
import { Link, Tabs } from "expo-router";
import { Mic, Send, Settings } from "lucide-react-native";
import { create } from "zustand";
import { useBootstrap, useMessages, useSendChat } from "@/lib/hooks";
import type { Message } from "@/lib/types";
import { timeAgo } from "@/lib/ui";
import { cn } from "@/lib/cn";

// Current console conversation (ephemeral — a new one starts each session).
const useChatStore = create<{ conversationId: string | null; setConversationId: (id: string) => void }>(
  (set) => ({
    conversationId: null,
    setConversationId: (id: string) => set({ conversationId: id }),
  })
);

function Bubble({ message }: { message: Message }) {
  const isUser = message.role === "user";
  const isSystem = message.role === "system";
  if (isSystem) {
    return (
      <Text className="text-center text-zinc-500 text-xs my-2 px-6" selectable>
        {message.content}
      </Text>
    );
  }
  return (
    <View className={cn("px-4 my-1", isUser ? "items-end" : "items-start")}>
      {!isUser && message.agent_id ? (
        <Text className="text-[10px] text-emerald-500 font-semibold mb-1 ml-1 uppercase tracking-wider">
          {message.agent_id}
        </Text>
      ) : null}
      <View
        className={cn(
          "rounded-2xl px-4 py-3 max-w-[85%]",
          isUser ? "bg-emerald-500 rounded-br-md" : "bg-zinc-900 border border-zinc-800 rounded-bl-md"
        )}
      >
        <Text className={cn("text-[15px] leading-5", isUser ? "text-zinc-950" : "text-zinc-100")} selectable>
          {message.content}
        </Text>
      </View>
      <Text className="text-[10px] text-zinc-600 mt-1 mx-1">{timeAgo(message.created_at)}</Text>
    </View>
  );
}

export default function ChatScreen() {
  const conversationId = useChatStore((s) => s.conversationId);
  const setConversationId = useChatStore((s) => s.setConversationId);
  const [draft, setDraft] = useState<string>("");

  const bootstrap = useBootstrap();
  const messages = useMessages(conversationId);
  const send = useSendChat();

  const online = !bootstrap.isError;
  const pending = bootstrap.data?.counts.approvals_pending ?? 0;

  const onSend = () => {
    const content = draft.trim();
    if (!content || send.isPending) return;
    setDraft("");
    send.mutate(
      { content, conversation_id: conversationId ?? undefined },
      { onSuccess: (res) => setConversationId(res.conversation_id) }
    );
  };

  return (
    <View className="flex-1 bg-zinc-950" testID="chat-screen">
      <Tabs.Screen
        options={{
          title: "monGARS",
          headerRight: () => (
            <Link href="/settings" asChild>
              <Pressable testID="open-settings-button" className="mr-4 active:opacity-50" hitSlop={12}>
                <Settings size={22} color="#a1a1aa" />
              </Pressable>
            </Link>
          ),
        }}
      />

      {/* Connection status strip */}
      <View className="flex-row items-center px-4 py-2 border-b border-zinc-900">
        <View className={cn("w-2 h-2 rounded-full mr-2", online ? "bg-emerald-400" : "bg-red-500")} />
        <Text className="text-zinc-400 text-xs flex-1">
          {online ? "Control plane en ligne" : "Control plane injoignable"}
          {pending > 0 ? ` · ${pending} approbation${pending > 1 ? "s" : ""} en attente` : ""}
        </Text>
        <Text className="text-zinc-600 text-[10px]">Ubuntu · SQLite WAL</Text>
      </View>

      <KeyboardAvoidingView
        className="flex-1"
        behavior={Platform.OS === "ios" ? "padding" : undefined}
        keyboardVerticalOffset={Platform.OS === "ios" ? 90 : 0}
      >
        <FlatList
          testID="chat-messages"
          data={[...(messages.data ?? [])].reverse()}
          keyExtractor={(m) => m.id}
          renderItem={({ item }) => <Bubble message={item} />}
          inverted
          contentContainerStyle={{ paddingVertical: 12, flexGrow: 1 }}
          ListEmptyComponent={
            <View className="flex-1 items-center justify-center px-8" style={{ transform: [{ scaleY: -1 }] }}>
              <Text className="text-zinc-200 text-xl font-bold text-center">Console du swarm</Text>
              <Text className="text-zinc-500 text-sm text-center mt-2 leading-5">
                Donne une intention. L'orchestrateur planifie, la gateway demande permission, les workers exécutent.
              </Text>
              <View className="mt-6 gap-2 w-full">
                {["Liste les fichiers du projet 27pm-crm", "Corrige le bug de sync dans client.ts"].map((s) => (
                  <Pressable
                    key={s}
                    testID={`suggestion-${s.slice(0, 10)}`}
                    onPress={() => setDraft(s)}
                    className="bg-zinc-900 border border-zinc-800 rounded-xl px-4 py-3 active:bg-zinc-800"
                  >
                    <Text className="text-zinc-300 text-sm">{s}</Text>
                  </Pressable>
                ))}
              </View>
            </View>
          }
        />

        {/* Input bar */}
        <View className="flex-row items-end px-3 py-2 border-t border-zinc-900 bg-zinc-950">
          <Pressable testID="mic-button" className="p-3 active:opacity-50" hitSlop={8}>
            <Mic size={20} color="#71717a" />
          </Pressable>
          <TextInput
            testID="chat-input"
            className="flex-1 bg-zinc-900 border border-zinc-800 rounded-2xl px-4 py-2.5 text-zinc-100 text-[15px] max-h-28"
            placeholder="Parle à ton swarm…"
            placeholderTextColor="#52525b"
            multiline
            value={draft}
            onChangeText={setDraft}
            onSubmitEditing={onSend}
          />
          <Pressable
            testID="send-button"
            onPress={onSend}
            disabled={!draft.trim() || send.isPending}
            className={cn(
              "ml-2 w-10 h-10 rounded-full items-center justify-center",
              draft.trim() ? "bg-emerald-500 active:bg-emerald-400" : "bg-zinc-800"
            )}
          >
            <Send size={18} color={draft.trim() ? "#09090b" : "#52525b"} />
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </View>
  );
}
