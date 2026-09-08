import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api/api";
import type {
  Agent,
  Approval,
  AuditEvent,
  Bootstrap,
  Conversation,
  MemoryItem,
  Message,
  Task,
  TaskDetail,
  TaskMode,
} from "./types";

// Live-ish sync: the MVP polls instead of WebSocket (Phase 1 → WS later).
const POLL = 3000;

export const useBootstrap = () =>
  useQuery({
    queryKey: ["bootstrap"],
    queryFn: () => api.get<Bootstrap>("/api/sync/bootstrap"),
    refetchInterval: POLL * 2,
  });

export const useTasks = (status?: string) =>
  useQuery({
    queryKey: ["tasks", status ?? "all"],
    queryFn: () => api.get<Task[]>(`/api/tasks${status ? `?status=${status}` : ""}`),
    refetchInterval: POLL,
  });

export const useTask = (id: string) =>
  useQuery({
    queryKey: ["task", id],
    queryFn: () => api.get<TaskDetail>(`/api/tasks/${id}`),
    refetchInterval: POLL,
    enabled: !!id,
  });

export const useApprovals = (status: string = "pending") =>
  useQuery({
    queryKey: ["approvals", status],
    queryFn: () => api.get<Approval[]>(`/api/approvals?status=${status}`),
    refetchInterval: POLL,
  });

export const useAgents = () =>
  useQuery({
    queryKey: ["agents"],
    queryFn: () => api.get<Agent[]>("/api/agents"),
    refetchInterval: POLL * 2,
  });

export const useMemory = () =>
  useQuery({
    queryKey: ["memory"],
    queryFn: () => api.get<MemoryItem[]>("/api/memory"),
  });

export const useConversations = () =>
  useQuery({
    queryKey: ["conversations"],
    queryFn: () => api.get<Conversation[]>("/api/conversations"),
  });

export const useMessages = (conversationId: string | null) =>
  useQuery({
    queryKey: ["messages", conversationId],
    queryFn: () => api.get<Message[]>(`/api/conversations/${conversationId}/messages`),
    enabled: !!conversationId,
    refetchInterval: POLL,
  });

export const useAudit = () =>
  useQuery({
    queryKey: ["audit"],
    queryFn: () => api.get<AuditEvent[]>("/api/sync/audit?limit=30"),
    refetchInterval: POLL * 2,
  });

// Mutations

export const useSendChat = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { content: string; conversation_id?: string }) =>
      api.post<{ conversation_id: string; task: Task }>("/api/chat", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["messages"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
};

export const useCreateTask = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { input: string; mode?: TaskMode }) => api.post<Task>("/api/tasks", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks"] }),
  });
};

export const useCancelTask = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Task>(`/api/tasks/${id}/cancel`, {}),
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["task", id] });
    },
  });
};

export const useApprovalDecision = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { id: string; decision: "allow_once" | "allow_rule" | "deny"; user_note?: string }) =>
      api.post<Approval>(`/api/approvals/${body.id}/decision`, {
        decision: body.decision,
        user_note: body.user_note,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["task"] });
      qc.invalidateQueries({ queryKey: ["messages"] });
    },
  });
};

export const useRemember = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { content: string; summary?: string; scope?: string; kind?: string; pinned?: boolean }) =>
      api.post<MemoryItem>("/api/memory/remember", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memory"] }),
  });
};

export const useSearchMemory = () =>
  useMutation({
    mutationFn: (query: string) => api.post<MemoryItem[]>("/api/memory/search", { query }),
  });

export const useUpdateMemory = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { id: string; pinned?: boolean; content?: string }) =>
      api.patch<MemoryItem>(`/api/memory/${body.id}`, { pinned: body.pinned, content: body.content }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memory"] }),
  });
};

export const useDeleteMemory = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<{ deleted: boolean }>(`/api/memory/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memory"] }),
  });
};

export const usePairing = () => {
  const start = useMutation({
    mutationFn: () => api.post<{ code: string; expires_at: string }>("/api/pairing/start", {}),
  });
  const confirm = useMutation({
    mutationFn: (body: { code: string; device_id: string; device_name: string }) =>
      api.post<{ device_token: string; device_id: string }>("/api/pairing/confirm", body),
  });
  return { start, confirm };
};

export const useFeedback = () => {
  return useMutation({
    mutationFn: (body: { task_id: string; score: number; label?: string }) =>
      api.post("/api/sync/feedback", { ...body, type: "rating" }),
  });
};
