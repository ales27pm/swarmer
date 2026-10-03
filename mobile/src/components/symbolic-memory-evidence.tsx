import { useState } from "react";
import { Text, View } from "react-native";

import { ActionButton, COLORS } from "./swarm-ui";
import { memorySymbolicEvidence, type SymbolicEvidence } from "@/lib/api/memory-symbolic";
import type { MemoryItem } from "@/lib/api/types";

function Proposal({ evidence }: { evidence: SymbolicEvidence }) {
  const [expanded, setExpanded] = useState(false);
  const proposal = evidence.proposal;
  return (
    <View style={{ borderColor: COLORS.border, borderTopWidth: 1, paddingTop: 12, gap: 8 }}>
      <Text style={{ color: COLORS.text, fontWeight: "700" }}>
        Proposition non validée{proposal.lifecycle === "superseded" ? " · remplacée" : ""}
      </Text>
      <Text style={{ color: COLORS.muted }}>Cette proposition reste à vérifier.</Text>
      <Text style={{ color: COLORS.subtle }}>
        {proposal.sources.length} source{proposal.sources.length > 1 ? "s" : ""} · {evidence.catalog.namespace} / {evidence.catalog.scheme_id}
      </Text>
      <ActionButton
        label={expanded ? "Masquer la proposition et ses sources" : "Voir la proposition et ses sources"}
        accessibilityLabel={`${expanded ? "Masquer" : "Voir"} la proposition ${proposal.proposal_id} et ses sources`}
        testID={`symbolic-details-${proposal.proposal_id}`}
        onPress={() => setExpanded((value) => !value)}
      />
      {expanded ? (
        <>
          <Text style={{ color: COLORS.muted }}>Proposition, conditions et provenance intégrales</Text>
          <Text selectable testID={`symbolic-content-${proposal.proposal_id}`} style={{ color: COLORS.text, fontSize: 12, lineHeight: 18 }}>
            {JSON.stringify({ proposal, concepts: evidence.concepts, relations: evidence.relations, matches: evidence.matches }, null, 2)}
          </Text>
        </>
      ) : null}
    </View>
  );
}

export function SymbolicMemoryEvidence({ item }: { item: MemoryItem }) {
  let evidence: SymbolicEvidence[];
  try { evidence = memorySymbolicEvidence(item); } catch {
    return <Text accessibilityRole="alert" style={{ color: COLORS.muted }}>Les propositions de cette mémoire sont indisponibles.</Text>;
  }
  return <>{evidence.map((entry) => <Proposal key={entry.proposal.proposal_id} evidence={entry} />)}</>;
}
