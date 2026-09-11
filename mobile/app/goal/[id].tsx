import { Stack, useLocalSearchParams, useRouter } from "expo-router";

import {
  GoalDetailContent,
  useGoalDetailController,
} from "@/screens/goal-detail-content";

function firstParam(value: string | string[] | undefined) {
  return Array.isArray(value) ? value[0] : value;
}

export default function GoalDetailScreen() {
  const { id } = useLocalSearchParams<{ id?: string | string[] }>();
  const router = useRouter();
  const controller = useGoalDetailController(firstParam(id));
  const navigation = {
    openApprovals: () => router.push("/approvals"),
    openTask: (taskId: string) => router.push({ pathname: "/task/[id]", params: { id: taskId } }),
    openGoal: (goalId: string) => router.push({ pathname: "/goal/[id]", params: { id: goalId } }),
  };

  return (
    <>
      <Stack.Screen options={{ title: "But" }} />
      <GoalDetailContent controller={controller} navigation={navigation} />
    </>
  );
}
