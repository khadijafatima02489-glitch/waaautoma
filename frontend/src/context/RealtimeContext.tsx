import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/context/AuthContext";

const RealtimeContext = createContext(true);
export function RealtimeProvider({ children }: { children: ReactNode }) {
  const { session } = useAuth(); const queryClient = useQueryClient();
  useEffect(() => {
    const token = localStorage.getItem("token"); if (!session || !token) return;
    const events = new EventSource(`/api/stream?token=${encodeURIComponent(token)}`);
    const isAdmin = session.user.role === "SUPER_ADMIN";
    events.onmessage = (event) => { try { const payload = JSON.parse(event.data) as { type: string }; if (isAdmin) { void queryClient.invalidateQueries({ queryKey: ["admin-summary"] }); void queryClient.invalidateQueries({ queryKey: ["admin-restaurants"] }); void queryClient.invalidateQueries({ queryKey: ["admin-subscriptions"] }); void queryClient.invalidateQueries({ queryKey: ["admin-restaurant-detail"] }); return; } if (["new_order", "order_update"].includes(payload.type)) { void queryClient.invalidateQueries({ queryKey: ["orders"] }); void queryClient.invalidateQueries({ queryKey: ["analytics"] }); } if (payload.type === "message") void queryClient.invalidateQueries({ queryKey: ["simulation"] }); } catch { /* ignore heartbeat */ } };
    return () => events.close();
  }, [session, queryClient]);
  return <RealtimeContext.Provider value>{children}</RealtimeContext.Provider>;
}
export function useRealtime() { return useContext(RealtimeContext); }