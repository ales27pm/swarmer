import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import { Keyboard, RefreshControl, ScrollView, Text, TextInput, View, type KeyboardEvent, type TextInputProps } from "react-native";

import { ActionButton, COLORS } from "@/components/swarm-ui";

type KeyboardScroll = {
  focus: (view: View, input: TextInput) => void;
  blur: (view: View) => void;
  reveal: (view: View) => void;
};
const KeyboardScrollContext = createContext<KeyboardScroll | null>(null);
const KeyboardGroupContext = createContext<{
  focus: (input: TextInput) => void;
  blur: () => void;
  reveal: () => void;
} | null>(null);

/** Group only the input and its controls, excluding surrounding history or help text. */
export function KeyboardInputGroup({ children, dismissKeyboard = false, testID }: PropsWithChildren<{
  dismissKeyboard?: boolean;
  testID?: string;
}>) {
  const scroll = useContext(KeyboardScrollContext);
  const view = useRef<View>(null);
  const [focused, setFocused] = useState(false);
  const group = useMemo(() => ({
    focus: (input: TextInput) => {
      setFocused(true);
      if (view.current) scroll?.focus(view.current, input);
    },
    blur: () => {
      setFocused(false);
      if (view.current) scroll?.blur(view.current);
    },
    reveal: () => {
      if (view.current) scroll?.reveal(view.current);
    },
  }), [scroll]);
  useEffect(() => {
    const target = view.current;
    return () => { if (target) scroll?.blur(target); };
  }, [scroll]);
  return (
    <KeyboardGroupContext.Provider value={group}>
      <View ref={view} collapsable={false} onLayout={group.reveal} style={{ gap: 10 }} testID={testID}>
        {children}
        {dismissKeyboard && focused ? (
          <ActionButton label="Fermer le clavier" onPress={() => Keyboard.dismiss()} />
        ) : null}
      </View>
    </KeyboardGroupContext.Provider>
  );
}

export function KeyboardTextInput(props: TextInputProps) {
  const group = useContext(KeyboardGroupContext);
  const input = useRef<TextInput>(null);
  return (
    <TextInput
      {...props}
      ref={input}
      onFocus={(event) => { if (input.current) group?.focus(input.current); props.onFocus?.(event); }}
      onBlur={(event) => { group?.blur(); props.onBlur?.(event); }}
      onContentSizeChange={(event) => { group?.reveal(); props.onContentSizeChange?.(event); }}
      onSelectionChange={(event) => { group?.reveal(); props.onSelectionChange?.(event); }}
    />
  );
}

export function ScreenShell({
  title,
  subtitle,
  children,
  onRefresh,
  refreshing = false,
  showTitle = true,
  testID,
}: PropsWithChildren<{
  title: string;
  showTitle?: boolean;
  subtitle?: string;
  onRefresh?: () => void;
  refreshing?: boolean;
  testID?: string;
}>) {
  const [expandedTitle, setExpandedTitle] = useState(false);
  const scrollView = useRef<ScrollView>(null);
  const activeGroup = useRef<View | null>(null);
  const activeInput = useRef<TextInput | null>(null);
  const keyboardFrame = useRef(Keyboard.metrics?.());
  const keyboardHiding = useRef(false);
  const offset = useRef(0);
  const frame = useRef<number | null>(null);
  const measurement = useRef(0);
  const cancelReveal = useCallback(() => {
    measurement.current += 1;
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
  }, []);
  const reveal = useCallback(() => {
    cancelReveal();
    const version = measurement.current;
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      const target = activeGroup.current;
      const keyboard = keyboardFrame.current;
      const scroll = scrollView.current;
      if (!target || !keyboard || !scroll) return;
      const nativeScroll = scroll.getNativeScrollRef();
      if (!nativeScroll) return;
      const current = () => version === measurement.current && activeGroup.current === target;
      nativeScroll.measureInWindow((_x, top, _width, height) => {
        if (!current()) return;
        target.measureInWindow((_targetX, targetTop, _targetWidth, targetHeight) => {
          if (!current()) return;
          // Window measurements include the navigation header; no full-screen assumption.
          const bottom = Math.min(top + height, keyboard.screenY) - 12;
          if (bottom <= top) return;
          const visibleHeight = bottom - top - 8;
          const scrollIntoView = (viewTop: number, viewHeight: number) => {
            if (!current()) return;
            const hiddenBelow = viewTop + viewHeight - bottom;
            // An oversized region cannot fit both edges; never alternate between them.
            const hiddenAbove = viewHeight <= visibleHeight ? Math.min(0, viewTop - top - 8) : 0;
            const delta = hiddenBelow > 0 ? hiddenBelow : hiddenAbove;
            if (Math.abs(delta) > 1) {
              scroll.scrollTo({ y: Math.max(0, offset.current + delta), animated: false });
            }
          };
          if (targetHeight > visibleHeight && activeInput.current) {
            activeInput.current.measureInWindow((_inputX, inputTop, _inputWidth, inputHeight) => {
              scrollIntoView(inputTop, inputHeight);
            });
          } else {
            scrollIntoView(targetTop, targetHeight);
          }
        });
      });
    });
  }, [cancelReveal]);
  const keyboardScroll = useMemo<KeyboardScroll>(() => ({
    focus: (view, input) => { activeGroup.current = view; activeInput.current = input; reveal(); },
    blur: (view) => {
      if (activeGroup.current === view) {
        activeGroup.current = null;
        activeInput.current = null;
        cancelReveal();
      }
    },
    reveal: (view) => { if (activeGroup.current === view) reveal(); },
  }), [cancelReveal, reveal]);
  useEffect(() => {
    const shown = Keyboard.addListener("keyboardDidShow", (event: KeyboardEvent) => {
      keyboardHiding.current = false;
      keyboardFrame.current = event.endCoordinates;
      reveal();
    });
    const changed = Keyboard.addListener("keyboardDidChangeFrame", (event: KeyboardEvent) => {
      if (keyboardHiding.current) return;
      keyboardFrame.current = event.endCoordinates;
      reveal();
    });
    const hide = () => {
      keyboardHiding.current = true;
      keyboardFrame.current = undefined;
      cancelReveal();
    };
    const hiding = Keyboard.addListener("keyboardWillHide", hide);
    const hidden = Keyboard.addListener("keyboardDidHide", hide);
    return () => {
      shown.remove(); changed.remove(); hiding.remove(); hidden.remove();
      activeGroup.current = null;
      activeInput.current = null;
      cancelReveal();
    };
  }, [cancelReveal, reveal]);
  return (
    <KeyboardScrollContext.Provider value={keyboardScroll}>
      <ScrollView
        ref={scrollView}
        accessibilityLanguage="fr-FR"
        style={{ backgroundColor: COLORS.background }}
        testID={testID}
        contentInsetAdjustmentBehavior="automatic"
        automaticallyAdjustKeyboardInsets
        contentContainerStyle={{ gap: 20, padding: 20, paddingBottom: 40, width: "100%", maxWidth: 760, alignSelf: "center" }}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="interactive"
        onContentSizeChange={reveal}
        onLayout={reveal}
        onScroll={(event) => { offset.current = event.nativeEvent.contentOffset.y; }}
        scrollEventThrottle={16}
        refreshControl={
          onRefresh ? (
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor={COLORS.accent}
            />
          ) : undefined
        }
      >
        <View style={{ gap: 6 }}>
          {showTitle ? <Text accessibilityRole="header" selectable numberOfLines={title.length > 150 && !expandedTitle ? 3 : undefined} style={{ color: COLORS.text, fontSize: 26, lineHeight: 32, fontWeight: "800" }}>
            {title}
          </Text> : null}
          {showTitle && title.length > 150 ? <ActionButton label={expandedTitle ? "Réduire l’objectif" : "Lire l’objectif complet"} onPress={() => setExpandedTitle(!expandedTitle)} /> : null}
          {subtitle ? (
            <Text selectable style={{ color: COLORS.muted, fontSize: 14, lineHeight: 20 }}>
              {subtitle}
            </Text>
          ) : null}
        </View>
        {children}
      </ScrollView>
    </KeyboardScrollContext.Provider>
  );
}
