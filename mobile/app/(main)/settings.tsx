import { useEffect, useState } from "react";
import { Button, Text, TextInput, View } from "react-native";
import * as SecureStore from "expo-secure-store";
import { ScreenShell } from "@/components/screen-shell";
import { getServerUrl, pairDevice, setServerUrl } from "@/lib/api/client";

const DEVICE_ID_KEY = "mongars.device_id";

async function deviceId() {
  let id = await SecureStore.getItemAsync(DEVICE_ID_KEY);
  if (!id) {
    id = `iphone_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    await SecureStore.setItemAsync(DEVICE_ID_KEY, id);
  }
  return id;
}

export default function SettingsScreen() {
  const [url, setUrl] = useState("");
  const [code, setCode] = useState("");
  const [status, setStatus] = useState("Non jumelé");

  useEffect(() => { void getServerUrl().then(setUrl); }, []);

  const save = async () => {
    await setServerUrl(url);
    setStatus("URL enregistrée");
  };

  const pair = async () => {
    try {
      await setServerUrl(url);
      await pairDevice(code.trim(), await deviceId());
      setStatus("Jumelage réussi");
      setCode("");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <ScreenShell title="Settings">
      <View style={{ gap: 14 }}>
        <Text selectable>Control plane Ubuntu</Text>
        <TextInput value={url} onChangeText={setUrl} autoCapitalize="none" autoCorrect={false} placeholder="http://192.168.1.10:8710" style={{ borderWidth: 1, borderRadius: 12, padding: 12, borderCurve: "continuous" }} />
        <Button title="Enregistrer l’URL" onPress={() => void save()} />
        <Text selectable>Code de jumelage à 6 chiffres</Text>
        <TextInput value={code} onChangeText={setCode} keyboardType="number-pad" maxLength={6} placeholder="123456" style={{ borderWidth: 1, borderRadius: 12, padding: 12, borderCurve: "continuous" }} />
        <Button title="Jumeler cet iPhone" disabled={code.length !== 6} onPress={() => void pair()} />
        <Text selectable>{status}</Text>
      </View>
    </ScreenShell>
  );
}
