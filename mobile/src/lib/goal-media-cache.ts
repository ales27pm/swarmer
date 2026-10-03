import { Directory, File, Paths } from "expo-file-system";
import { Platform } from "react-native";
import type { GoalMediaArtifact } from "@/lib/api/goal-media";

let initialized = false;
let serial = 0;
/** Private, short-lived files. Nothing is placed in user-visible Documents. */
export function createGoalMediaCache() {
  let closed = false;
  const urls = new Set<string>();
  let directory: Directory | undefined;
  if (Platform.OS !== "web") {
    const root = new Directory(Paths.cache, "goal-media-v1");
    if (!initialized) {
      // A prior process may have exited without unmounting its preview.
      if (root.exists) root.delete();
      initialized = true;
    }
    root.create({ idempotent: true, intermediates: true });
    directory = new Directory(root, `${Date.now()}-${++serial}`);
    directory.create();
  }
  return {
    put(bytes: Uint8Array, artifact: GoalMediaArtifact): string {
      if (closed) throw new Error("L’aperçu est fermé.");
      if (Platform.OS === "web") {
        const uri = URL.createObjectURL(new Blob([bytes.slice().buffer], { type: artifact.media_type }));
        urls.add(uri); return uri;
      }
      const file = new File(directory!, `media.${artifact.media_type === "image/png" ? "png" : "wav"}`);
      file.create(); file.write(bytes);
      return file.uri;
    },
    dispose() {
      if (closed) return;
      closed = true;
      for (const uri of urls) URL.revokeObjectURL(uri);
      urls.clear();
      if (directory?.exists) directory.delete();
    },
  };
}
