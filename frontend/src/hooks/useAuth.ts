"use client";
import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";

interface AuthState { token:string|null; email:string|null; isAuthenticated:boolean; loading:boolean; error:string|null; }

export function useAuth() {
  const [s, set] = useState<AuthState>({ token:null, email:null, isAuthenticated:false, loading:false, error:null });

  useEffect(() => {
    try {
      const t = sessionStorage.getItem("cipher_token");
      const e = sessionStorage.getItem("cipher_email");
      if (t) set(x=>({...x,token:t,email:e,isAuthenticated:true}));
    } catch {}
  },[]);

  const login = useCallback(async (email:string,password:string)=>{
    set(x=>({...x,loading:true,error:null}));
    try {
      const d = await api.login(email,password);
      try { sessionStorage.setItem("cipher_token",d.access_token); sessionStorage.setItem("cipher_email",email); } catch {}
      set({token:d.access_token,email,isAuthenticated:true,loading:false,error:null});
    } catch(e:unknown){ set(x=>({...x,loading:false,error:e instanceof Error?e.message:"Auth failed"})); }
  },[]);

  const register = useCallback(async (email:string,password:string)=>{
    set(x=>({...x,loading:true,error:null}));
    try { await api.register(email,password); await login(email,password); }
    catch(e:unknown){ set(x=>({...x,loading:false,error:e instanceof Error?e.message:"Register failed"})); }
  },[login]);

  const logout = useCallback(()=>{
    try { sessionStorage.removeItem("cipher_token"); sessionStorage.removeItem("cipher_email"); } catch {}
    set({token:null,email:null,isAuthenticated:false,loading:false,error:null});
    window.location.href="/";
  },[]);

  return {...s,login,register,logout};
}
