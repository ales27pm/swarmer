import { useCallback, useEffect, useState } from "react";
import { Button, RefreshControl, ScrollView, Text, View } from "react-native";
import { ScreenShell } from "@/components/screen-shell";
import { Approval, bootstrapSync, decideApproval, listApprovals } from "@/lib/api/client";

export default function ApprovalsScreen() {
  const [items, setItems] = useState<Approval[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      await bootstrapSync();
      setItems(await listApprovals());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const decide = async (id: string, decision: "approve" | "deny") => {
    try {
      await decideApproval(id, decision);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <ScreenShell title="Approvals">
      <ScrollView contentInsetAdjustmentBehavior="automatic" refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} />} contentContainerStyle={{ gap: 14 }}>
        {error ? <Text selectable>{error}</Text> : null}
        {!items.length && !refreshing ? <Text selectable>Aucune permission en attente.</Text> : null}
        {items.map((item) => (
          <View key={item.id} style={{ gap: 8, padding: 14, borderWidth: 1, borderRadius: 14, borderCurve: "continuous" }}>
            <Text selectable style={{ fontWeight: "700" }}>{item.action}</Text>
            <Text selectable>{item.summary}</Text>
            <Text selectable>Risque: {item.risk}</Text>
            <View style={{ flexDirection: "row", gap: 10 }}>
              <Button title="Autoriser" onPress={() => void decide(item.id, "approve")} />
              <Button title="Refuser" onPress={() => void decide(item.id, "deny")} />
            </View>
          </View>
        ))}
      </ScrollView>
    </ScreenShell>
  );
}
