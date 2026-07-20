"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { User } from "@supabase/supabase-js";
import { supabaseBrowser } from "@/lib/supabase/client";
import { useLang } from "./lang-context";

export function AuthButton() {
  const { t } = useLang();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const supabase = supabaseBrowser();
    supabase.auth.getUser().then(({ data }) => {
      setUser(data.user ?? null);
      setReady(true);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_e, session) => {
      setUser(session?.user ?? null);
    });
    return () => sub.subscription.unsubscribe();
  }, []);

  async function signIn() {
    const supabase = supabaseBrowser();
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: `${window.location.origin}/auth/callback` },
    });
  }

  async function signOut() {
    const supabase = supabaseBrowser();
    await supabase.auth.signOut();
    router.refresh();
  }

  if (!ready) return <div className="h-8 w-16" aria-hidden />;

  return user ? (
    <button
      onClick={signOut}
      className="rounded-full border border-line px-3.5 py-1.5 text-xs text-sand-dim transition-colors duration-200 hover:border-sand-faint hover:text-sand"
    >
      {t.signOut}
    </button>
  ) : (
    <button
      onClick={signIn}
      className="rounded-full border border-line px-3.5 py-1.5 text-xs text-sand-dim transition-colors duration-200 hover:border-sand-faint hover:text-sand"
    >
      {t.signIn}
    </button>
  );
}
