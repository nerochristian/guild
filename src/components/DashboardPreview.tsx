import React, { useState } from "react";
import { LayoutDashboard, ShieldAlert, Settings, BarChart3, HelpCircle, Activity, ChevronRight, Check, X, ShieldCheck, ToggleLeft, ToggleRight, Trash2, Calendar, Sparkles, Server, FileText, LayoutGrid, TrendingUp, Users, Gavel, Shield, Grid, Scale, UserPlus, Bell } from "lucide-react";
import { motion, AnimatePresence } from "motion/react";
import { CaseLog } from "../types";

const INITIAL_CASES: CaseLog[] = [
  {
    id: "4810",
    user: "AlphaSpammer#1129",
    userId: "41924109",
    moderator: "Docket_AI",
    action: "BAN",
    reason: "Severe scam link dissemination (Phishing)",
    timestamp: "11:29:45 AM",
    severity: "high",
    aiSummary: "Automated analysis matched known Russian redirect. Banned on first violation."
  },
  {
    id: "4809",
    user: "ToxicGamer#8841",
    userId: "88412093",
    moderator: "ModChloe",
    action: "WARN",
    reason: "Repeated toxic slurs in #general after verbal warning",
    timestamp: "11:24:10 AM",
    severity: "medium",
    aiSummary: "Context evaluation classified 84% toxic confidence score."
  },
  {
    id: "4808",
    user: "AdBot_404#2023",
    userId: "20231924",
    moderator: "Docket_AI",
    action: "MUTE",
    reason: "Invite link spam in off-topic channel",
    timestamp: "11:15:32 AM",
    severity: "low",
    aiSummary: "Sent 5 invite links within 2.8 seconds. Muted for 1 hour."
  },
  {
    id: "4807",
    user: "RegularDude#0001",
    userId: "00019284",
    moderator: "ModJack",
    action: "RESOLVED",
    reason: "Appeal approved: Account was compromised and recovered",
    timestamp: "11:02:15 AM",
    severity: "medium",
    aiSummary: "Manual staff intervention. Verification matched original owner login."
  }
];

interface AppealItem {
  id: string;
  user: string;
  reason: string;
  action: string;
  status: "pending" | "approved" | "rejected";
}

const INITIAL_APPEALS: AppealItem[] = [
  { id: "app-1", user: "SadGamer#4040", reason: "I was super angry during the clan match, I apologize for typing that. I won't do it again.", action: "MUTE (6h)", status: "pending" },
  { id: "app-2", user: "FriendlyBot#1234", reason: "My account got hacked by a phishing bot. I have added 2FA now.", action: "BAN (Perm)", status: "pending" },
];

export default function DashboardPreview() {
  const [activeTab, setActiveTab] = useState<string>("overview");
  const [cases, setCases] = useState<CaseLog[]>(INITIAL_CASES);
  const [appeals, setAppeals] = useState<AppealItem[]>(INITIAL_APPEALS);

  const [automodConfig, setAutomodConfig] = useState({
    raidShield: true,
    spamFilter: true,
    scamDetector: true,
    toxicityFilter: false,
    verificationCaptcha: true,
  });

  const toggleConfig = (key: keyof typeof automodConfig) => {
    setAutomodConfig(prev => ({
      ...prev,
      [key]: !prev[key]
    }));
  };

  const handleAppeal = (id: string, decision: "approved" | "rejected") => {
    setAppeals(prev =>
      prev.map(app => (app.id === id ? { ...app, status: decision } : app))
    );
  };

  const deleteCase = (id: string) => {
    setCases(prev => prev.filter(c => c.id !== id));
  };

  return (
    <section id="dashboard" className="relative py-28 bg-[#05070E] overflow-hidden border-t border-white/10">
      
      {/* Background Lighting */}
      <div className="absolute top-1/4 right-1/4 w-[450px] h-[450px] bg-purple-600/10 rounded-full blur-[140px] pointer-events-none" />
      <div className="absolute bottom-1/4 left-1/4 w-[400px] h-[400px] bg-cyan-500/10 rounded-full blur-[140px] pointer-events-none" />

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 relative z-10">
        
        {/* Title Header */}
        <div className="text-center max-w-3xl mx-auto mb-16">
          <div className="inline-flex items-center space-x-2 bg-cyan-500/10 border border-cyan-500/30 text-cyan-300 py-1.5 px-4 rounded-full text-xs font-mono mb-4">
            <LayoutDashboard className="w-4 h-4 text-cyan-400" />
            <span>INTERACTIVE CLOUD COCKPIT</span>
          </div>
          <h2 className="font-heading font-extrabold text-3xl sm:text-5xl text-white tracking-tight mb-4">
            Manage server security from one <span className="bg-gradient-to-r from-cyan-400 via-indigo-300 to-purple-400 bg-clip-text text-transparent">central control room</span>.
          </h2>
          <p className="font-sans text-slate-300 text-base sm:text-lg leading-relaxed">
            No complicated Discord text prompts required. Set thresholds, analyze real-time infraction metrics, review appeals, and view full case history through our ultra-responsive web dashboard interface.
          </p>
        </div>

        {/* Dashboard Mockup Window */}
        <div className="glow-card rounded-3xl overflow-hidden shadow-2xl flex flex-col md:flex-row h-[560px] text-left border border-white/10">
          
          {/* Left Sidebar - Clean navigation items matching reference aesthetic without toggles */}
          <div className="w-full md:w-72 bg-[#0B0D17] border-r border-white/10 flex flex-col p-4 space-y-4 shrink-0 justify-between select-none">
            
            <div className="space-y-4">
              {/* Discord Bot Avatar Header */}
              <div className="flex items-center px-2 pt-1 pb-2">
                <div className="relative w-11 h-11 rounded-2xl bg-gradient-to-tr from-purple-950 via-slate-900 to-indigo-950 border border-purple-500/30 flex items-center justify-center shadow-lg group">
                  <div className="absolute inset-0 bg-purple-500/20 rounded-2xl blur-md group-hover:blur-lg transition-all" />
                  <svg className="w-6 h-6 text-indigo-300 relative z-10" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994.021-.041.001-.09-.041-.106a13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.061 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.028zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z" />
                  </svg>
                </div>
              </div>

              {/* Main Navigation Top Items */}
              <div className="space-y-1 font-sans text-xs">
                <button
                  onClick={() => setActiveTab("overview")}
                  className={`w-full flex items-center space-x-3 px-3.5 py-2.5 rounded-xl transition-all cursor-pointer font-medium ${
                    activeTab === "overview" ? "text-white bg-white/10 font-bold" : "text-slate-400 hover:text-white hover:bg-white/5"
                  }`}
                >
                  <LayoutGrid className="w-4 h-4 text-slate-400" />
                  <span>Dashboard</span>
                </button>

                <button
                  onClick={() => setActiveTab("cases")}
                  className={`w-full flex items-center space-x-3 px-3.5 py-2.5 rounded-xl transition-all cursor-pointer font-medium ${
                    activeTab === "cases" ? "text-white bg-white/10 font-bold" : "text-slate-400 hover:text-white hover:bg-white/5"
                  }`}
                >
                  <Server className="w-4 h-4 text-slate-400" />
                  <span>Servers</span>
                </button>

                <button
                  onClick={() => setActiveTab("appeals")}
                  className={`w-full flex items-center space-x-3 px-3.5 py-2.5 rounded-xl transition-all cursor-pointer font-medium ${
                    activeTab === "appeals" ? "text-white bg-white/10 font-bold" : "text-slate-400 hover:text-white hover:bg-white/5"
                  }`}
                >
                  <HelpCircle className="w-4 h-4 text-slate-400" />
                  <span>Support</span>
                </button>

                <button
                  onClick={() => setActiveTab("analytics")}
                  className={`w-full flex items-center space-x-3 px-3.5 py-2.5 rounded-xl transition-all cursor-pointer font-medium ${
                    activeTab === "analytics" ? "text-white bg-white/10 font-bold" : "text-slate-400 hover:text-white hover:bg-white/5"
                  }`}
                >
                  <Sparkles className="w-4 h-4 text-slate-400" />
                  <span>Premium</span>
                </button>
              </div>

              {/* Divider Line */}
              <div className="h-[1px] bg-white/10 my-3 w-full" />

              {/* Navigation Module List */}
              <div className="space-y-1 font-sans text-xs">
                
                {/* 1. Bot Settings */}
                <button
                  onClick={() => setActiveTab("automod")}
                  className="w-full flex items-center space-x-3 px-3.5 py-2.5 text-slate-300 hover:text-white hover:bg-white/5 rounded-xl cursor-pointer transition-all font-medium"
                >
                  <Settings className="w-4 h-4 text-slate-400" />
                  <span>Bot Settings</span>
                </button>

                {/* 2. Button Roles (Highlighted Active pill matching screenshot styling) */}
                <button
                  onClick={() => setActiveTab("automod")}
                  className="w-full flex items-center space-x-3 px-3.5 py-2.5 bg-gradient-to-r from-purple-600 to-indigo-600 text-white rounded-2xl cursor-pointer shadow-[0_0_20px_rgba(147,51,234,0.4)] font-bold transition-all"
                >
                  <div className="w-5 h-5 rounded-lg bg-white/20 flex items-center justify-center">
                    <Sparkles className="w-3.5 h-3.5 text-white" />
                  </div>
                  <span>Button Roles</span>
                </button>

                {/* 3. Verification / Greetings */}
                <button
                  onClick={() => setActiveTab("automod")}
                  className="w-full flex items-center space-x-3 px-3.5 py-2.5 text-slate-300 hover:text-white hover:bg-white/5 rounded-xl cursor-pointer transition-all font-medium"
                >
                  <ShieldCheck className="w-4 h-4 text-slate-400" />
                  <span>Verification/Greetings</span>
                </button>

                {/* 4. Custom Commands */}
                <button
                  onClick={() => setActiveTab("automod")}
                  className="w-full flex items-center space-x-3 px-3.5 py-2.5 text-slate-400 hover:text-white hover:bg-white/5 rounded-xl cursor-pointer transition-all font-medium"
                >
                  <ShieldAlert className="w-4 h-4 text-slate-500" />
                  <span>Custom Commands</span>
                </button>

                {/* 5. Timed Messages */}
                <button
                  onClick={() => setActiveTab("automod")}
                  className="w-full flex items-center space-x-3 px-3.5 py-2.5 text-slate-300 hover:text-white hover:bg-white/5 rounded-xl cursor-pointer transition-all font-medium"
                >
                  <Activity className="w-4 h-4 text-slate-400" />
                  <span>Timed Messages</span>
                </button>

                {/* 6. Command Moderation */}
                <button
                  onClick={() => setActiveTab("automod")}
                  className="w-full flex items-center space-x-3 px-3.5 py-2.5 text-slate-300 hover:text-white hover:bg-white/5 rounded-xl cursor-pointer transition-all font-medium"
                >
                  <BarChart3 className="w-4 h-4 text-slate-400" />
                  <span>Command Moderation</span>
                </button>

                {/* 7. Auto Moderation */}
                <button
                  onClick={() => setActiveTab("automod")}
                  className="w-full flex items-center space-x-3 px-3.5 py-2.5 text-slate-400 hover:text-white hover:bg-white/5 rounded-xl cursor-pointer transition-all font-medium"
                >
                  <ShieldAlert className="w-4 h-4 text-slate-500" />
                  <span>Auto Moderation</span>
                </button>

                {/* 8. Audit Logging */}
                <button
                  onClick={() => setActiveTab("cases")}
                  className="w-full flex items-center space-x-3 px-3.5 py-2.5 text-slate-300 hover:text-white hover:bg-white/5 rounded-xl cursor-pointer transition-all font-medium"
                >
                  <ChevronRight className="w-4 h-4 text-slate-400" />
                  <span>Audit Logging</span>
                </button>

              </div>
            </div>

            {/* Bottom Upgrade Now Neon CTA Banner matching screenshot */}
            <div className="relative mt-4 bg-gradient-to-r from-teal-400 via-emerald-400 to-cyan-400 rounded-2xl p-3.5 flex items-center justify-between shadow-[0_0_20px_rgba(45,212,191,0.4)] text-slate-950 overflow-hidden cursor-pointer hover:scale-[1.02] transition-transform">
              <div className="flex items-center space-x-2.5">
                <div className="w-8 h-8 rounded-xl bg-slate-950/20 backdrop-blur-sm flex items-center justify-center">
                  <Sparkles className="w-5 h-5 text-slate-950" />
                </div>
                <span className="font-heading font-extrabold text-sm text-slate-950 tracking-tight">
                  Upgrade Now
                </span>
              </div>
              <div className="w-6 h-6 rounded-full bg-white flex items-center justify-center shadow-md">
                <ChevronRight className="w-4 h-4 text-slate-950" />
              </div>
            </div>

          </div>

          {/* Right Content */}
          <div className="flex-1 bg-[#05070E]/80 backdrop-blur-xl overflow-y-auto p-6 text-slate-300">
            
            {/* OVERVIEW */}
            {activeTab === "overview" && (
              <div className="space-y-6">
                <div>
                  <h4 className="font-heading font-extrabold text-xl text-white">Server Intelligence Briefing</h4>
                  <p className="font-sans text-xs text-slate-400">Real-time threat status and member telemetry data.</p>
                </div>

                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                  <div className="glass-panel p-4 rounded-2xl border border-white/5">
                    <span className="text-[10px] font-mono text-slate-400 uppercase tracking-wider block">Total Guild Members</span>
                    <p className="font-heading font-extrabold text-2xl text-white mt-1">14,285</p>
                    <span className="text-[10px] text-emerald-400 font-sans block mt-1">+142 this week</span>
                  </div>
                  <div className="glass-panel p-4 rounded-2xl border border-white/5">
                    <span className="text-[10px] font-mono text-slate-400 uppercase tracking-wider block">Security Status</span>
                    <p className="font-heading font-extrabold text-2xl text-emerald-400 mt-1">CONTAINED</p>
                    <span className="text-[10px] text-slate-400 font-sans block mt-1">0 active attacks</span>
                  </div>
                  <div className="glass-panel p-4 rounded-2xl border border-white/5">
                    <span className="text-[10px] font-mono text-slate-400 uppercase tracking-wider block">Total Mod Cases</span>
                    <p className="font-heading font-extrabold text-2xl text-white mt-1">412</p>
                    <span className="text-[10px] text-purple-300 font-sans block mt-1">99.2% AI Auto-Resolved</span>
                  </div>
                  <div className="glass-panel p-4 rounded-2xl border border-white/5">
                    <span className="text-[10px] font-mono text-slate-400 uppercase tracking-wider block">Neural Shield</span>
                    <p className="font-heading font-extrabold text-2xl text-cyan-400 mt-1">ENABLED</p>
                    <span className="text-[10px] text-slate-400 font-sans block mt-1">Confidence Target: 95%</span>
                  </div>
                </div>

                <div className="glass-panel rounded-2xl p-5 space-y-3 border border-white/10">
                  <div className="flex justify-between items-center pb-3 border-b border-white/5">
                    <span className="font-mono text-xs text-purple-300 font-bold tracking-wider">LIVE TELEMETRY STREAM</span>
                    <span className="text-[10px] font-mono text-emerald-400">ACTIVE FEED</span>
                  </div>
                  <div className="space-y-2 text-xs font-mono">
                    <div className="flex justify-between hover:bg-white/5 p-2 rounded-xl transition-all">
                      <span className="text-purple-300">• AUTOMOD RULE #03: Phishing domain redirect blocked</span>
                      <span className="text-slate-500">2 mins ago</span>
                    </div>
                    <div className="flex justify-between hover:bg-white/5 p-2 rounded-xl transition-all">
                      <span className="text-cyan-300">• RAID LOCKDOWN: Joint verification engaged</span>
                      <span className="text-slate-500">14 mins ago</span>
                    </div>
                    <div className="flex justify-between hover:bg-white/5 p-2 rounded-xl transition-all">
                      <span className="text-amber-300">• INFRACTION LOG #4812: Mute logged for @Scammer</span>
                      <span className="text-slate-500">22 mins ago</span>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* CASES */}
            {activeTab === "cases" && (
              <div className="space-y-4">
                <div className="flex justify-between items-center">
                  <div>
                    <h4 className="font-heading font-extrabold text-xl text-white">Sharded Infraction Logs</h4>
                    <p className="font-sans text-xs text-slate-400">Auditable, immutable case records for this guild shard.</p>
                  </div>
                  <span className="text-xs bg-[#090D1A] border border-white/10 px-3 py-1.5 rounded-xl text-purple-300 font-mono font-semibold">
                    Shards Connected: 24
                  </span>
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full text-xs text-left border-collapse">
                    <thead>
                      <tr className="border-b border-white/10 text-slate-400 font-mono">
                        <th className="py-3 px-2">CASE ID</th>
                        <th className="py-3 px-2">MEMBER</th>
                        <th className="py-3 px-2">ENFORCEMENT</th>
                        <th className="py-3 px-2">REASON</th>
                        <th className="py-3 px-2">TIME</th>
                        <th className="py-3 px-2 text-right">ACTION</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/5">
                      {cases.map((c) => (
                        <tr key={c.id} className="hover:bg-white/5 transition-colors group">
                          <td className="py-3 px-2 font-mono font-bold text-purple-400">#{c.id}</td>
                          <td className="py-3 px-2 font-sans font-bold text-white">{c.user}</td>
                          <td className="py-3 px-2 font-mono">
                            <span className={`px-2.5 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider ${
                              c.action === "BAN" ? "bg-rose-500/20 text-rose-300 border border-rose-500/40" :
                              c.action === "MUTE" ? "bg-purple-500/20 text-purple-300 border border-purple-500/40" :
                              c.action === "WARN" ? "bg-amber-500/20 text-amber-300 border border-amber-500/40" :
                              "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40"
                            }`}>
                              {c.action}
                            </span>
                          </td>
                          <td className="py-3 px-2 text-slate-300 max-w-xs truncate">{c.reason}</td>
                          <td className="py-3 px-2 text-slate-400 font-mono text-[11px]">{c.timestamp}</td>
                          <td className="py-3 px-2 text-right">
                            <button
                              onClick={() => deleteCase(c.id)}
                              className="text-slate-400 hover:text-rose-400 transition-colors p-1.5 rounded-lg hover:bg-rose-500/10 cursor-pointer"
                              title="Delete case record"
                            >
                              <Trash2 className="w-4 h-4" />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* AUTOMOD */}
            {activeTab === "automod" && (
              <div className="space-y-6">
                <div>
                  <h4 className="font-heading font-extrabold text-xl text-white">Automod Engine Switches</h4>
                  <p className="font-sans text-xs text-slate-400">Enable or tune active AI shield filters in real time.</p>
                </div>

                <div className="space-y-3.5">
                  <div className="glass-panel p-4 rounded-2xl border border-white/10 flex items-center justify-between">
                    <div>
                      <span className="font-heading font-bold text-sm text-white block">Raid Protection Lock</span>
                      <span className="font-sans text-xs text-slate-400">Engages anti-bot queue if member join velocity exceeds threshold.</span>
                    </div>
                    <button onClick={() => toggleConfig("raidShield")} className="cursor-pointer">
                      {automodConfig.raidShield ? (
                        <ToggleRight className="w-9 h-9 text-purple-400 drop-shadow-[0_0_10px_rgba(168,85,247,0.6)]" />
                      ) : (
                        <ToggleLeft className="w-9 h-9 text-slate-600" />
                      )}
                    </button>
                  </div>

                  <div className="glass-panel p-4 rounded-2xl border border-white/10 flex items-center justify-between">
                    <div>
                      <span className="font-heading font-bold text-sm text-white block">Unsolicited Invite Shield</span>
                      <span className="font-sans text-xs text-slate-400">Deletes unauthorized server invite links instantly.</span>
                    </div>
                    <button onClick={() => toggleConfig("spamFilter")} className="cursor-pointer">
                      {automodConfig.spamFilter ? (
                        <ToggleRight className="w-9 h-9 text-purple-400 drop-shadow-[0_0_10px_rgba(168,85,247,0.6)]" />
                      ) : (
                        <ToggleLeft className="w-9 h-9 text-slate-600" />
                      )}
                    </button>
                  </div>

                  <div className="glass-panel p-4 rounded-2xl border border-white/10 flex items-center justify-between">
                    <div>
                      <span className="font-heading font-bold text-sm text-white block">Anti-Phishing Heuristic Engine</span>
                      <span className="font-sans text-xs text-slate-400">Destroys fraudulent domains imitating Nitro or wallet sites.</span>
                    </div>
                    <button onClick={() => toggleConfig("scamDetector")} className="cursor-pointer">
                      {automodConfig.scamDetector ? (
                        <ToggleRight className="w-9 h-9 text-purple-400 drop-shadow-[0_0_10px_rgba(168,85,247,0.6)]" />
                      ) : (
                        <ToggleLeft className="w-9 h-9 text-slate-600" />
                      )}
                    </button>
                  </div>

                  <div className="glass-panel p-4 rounded-2xl border border-white/10 flex items-center justify-between">
                    <div>
                      <span className="font-heading font-bold text-sm text-white block">NLP Sentiment Harassment Filter</span>
                      <span className="font-sans text-xs text-slate-400">Classifies hostility and slurs with context awareness.</span>
                    </div>
                    <button onClick={() => toggleConfig("toxicityFilter")} className="cursor-pointer">
                      {automodConfig.toxicityFilter ? (
                        <ToggleRight className="w-9 h-9 text-purple-400 drop-shadow-[0_0_10px_rgba(168,85,247,0.6)]" />
                      ) : (
                        <ToggleLeft className="w-9 h-9 text-slate-600" />
                      )}
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* ANALYTICS */}
            {activeTab === "analytics" && (
              <div className="space-y-6">
                <div>
                  <h4 className="font-heading font-extrabold text-xl text-white">Security Analytics</h4>
                  <p className="font-sans text-xs text-slate-400">Real-time threat graphs and prevention metric trends.</p>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="glass-panel p-5 rounded-2xl border border-white/10">
                    <span className="text-[10px] font-mono text-purple-300 uppercase font-bold block mb-3">Threat Interceptions (Weekly)</span>
                    <div className="h-32 flex items-end space-x-3 pt-4">
                      <div className="flex-1 bg-purple-500/20 hover:bg-purple-500/40 transition-colors h-16 rounded-t-lg relative group">
                        <span className="absolute -top-6 left-1/2 -translate-x-1/2 bg-purple-950 border border-purple-500 text-[10px] px-1.5 rounded opacity-0 group-hover:opacity-100 transition-opacity">14</span>
                      </div>
                      <div className="flex-1 bg-purple-500/30 hover:bg-purple-500/50 transition-colors h-24 rounded-t-lg relative group">
                        <span className="absolute -top-6 left-1/2 -translate-x-1/2 bg-purple-950 border border-purple-500 text-[10px] px-1.5 rounded opacity-0 group-hover:opacity-100 transition-opacity">22</span>
                      </div>
                      <div className="flex-1 bg-purple-500/20 hover:bg-purple-500/40 transition-colors h-12 rounded-t-lg relative group">
                        <span className="absolute -top-6 left-1/2 -translate-x-1/2 bg-purple-950 border border-purple-500 text-[10px] px-1.5 rounded opacity-0 group-hover:opacity-100 transition-opacity">9</span>
                      </div>
                      <div className="flex-1 bg-purple-500/60 hover:bg-purple-500/80 transition-colors h-28 rounded-t-lg relative group">
                        <span className="absolute -top-6 left-1/2 -translate-x-1/2 bg-purple-950 border border-purple-500 text-[10px] px-1.5 rounded opacity-0 group-hover:opacity-100 transition-opacity">34</span>
                      </div>
                      <div className="flex-1 bg-purple-500/40 hover:bg-purple-500/60 transition-colors h-18 rounded-t-lg relative group">
                        <span className="absolute -top-6 left-1/2 -translate-x-1/2 bg-purple-950 border border-purple-500 text-[10px] px-1.5 rounded opacity-0 group-hover:opacity-100 transition-opacity">16</span>
                      </div>
                      <div className="flex-1 bg-cyan-400 hover:bg-cyan-300 transition-colors h-32 rounded-t-lg relative group shadow-[0_0_15px_rgba(34,211,238,0.5)]">
                        <span className="absolute -top-6 left-1/2 -translate-x-1/2 bg-cyan-950 border border-cyan-400 text-[10px] px-1.5 rounded opacity-0 group-hover:opacity-100 transition-opacity font-bold">45</span>
                      </div>
                    </div>
                    <div className="flex justify-between text-[11px] text-slate-400 font-mono mt-3">
                      <span>Mon</span>
                      <span>Tue</span>
                      <span>Wed</span>
                      <span>Thu</span>
                      <span>Fri</span>
                      <span className="text-cyan-300 font-bold">Today</span>
                    </div>
                  </div>

                  <div className="glass-panel p-5 rounded-2xl border border-white/10 space-y-4">
                    <span className="text-[10px] font-mono text-cyan-300 uppercase font-bold block">Threat Distribution</span>
                    <div className="space-y-3">
                      <div>
                        <div className="flex justify-between text-xs text-slate-300 mb-1 font-semibold">
                          <span>Crypto & Phishing Scams</span>
                          <span className="text-purple-400 font-mono">42%</span>
                        </div>
                        <div className="w-full bg-slate-900 h-2 rounded-full overflow-hidden">
                          <div className="bg-purple-500 h-full rounded-full" style={{ width: "42%" }} />
                        </div>
                      </div>
                      <div>
                        <div className="flex justify-between text-xs text-slate-300 mb-1 font-semibold">
                          <span>Invite & Promo Spam</span>
                          <span className="text-cyan-400 font-mono">34%</span>
                        </div>
                        <div className="w-full bg-slate-900 h-2 rounded-full overflow-hidden">
                          <div className="bg-cyan-400 h-full rounded-full" style={{ width: "34%" }} />
                        </div>
                      </div>
                      <div>
                        <div className="flex justify-between text-xs text-slate-300 mb-1 font-semibold">
                          <span>Toxicity & Slur Violations</span>
                          <span className="text-amber-400 font-mono">24%</span>
                        </div>
                        <div className="w-full bg-slate-900 h-2 rounded-full overflow-hidden">
                          <div className="bg-amber-400 h-full rounded-full" style={{ width: "24%" }} />
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* APPEALS */}
            {activeTab === "appeals" && (
              <div className="space-y-4">
                <div>
                  <h4 className="font-heading font-extrabold text-xl text-white">Pending Appeals Ticket Queue</h4>
                  <p className="font-sans text-xs text-slate-400">Review restricted user responses under sandbox conditions.</p>
                </div>

                <div className="space-y-3">
                  {appeals.map((app) => (
                    <div key={app.id} className="glass-panel p-4 rounded-2xl border border-white/10 flex flex-col md:flex-row justify-between md:items-center gap-4">
                      <div className="space-y-1">
                        <div className="flex items-center space-x-2">
                          <span className="font-heading font-bold text-sm text-white">{app.user}</span>
                          <span className="text-[10px] font-mono bg-purple-500/20 text-purple-200 px-2 py-0.5 rounded-full border border-purple-500/30 font-semibold">
                            {app.action}
                          </span>
                        </div>
                        <p className="font-sans text-xs text-slate-300 italic">"{app.reason}"</p>
                      </div>

                      <div className="flex items-center space-x-2 justify-end">
                        {app.status === "pending" ? (
                          <>
                            <button
                              onClick={() => handleAppeal(app.id, "approved")}
                              className="bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-300 border border-emerald-500/40 px-3 py-1.5 rounded-xl text-xs font-bold flex items-center space-x-1 cursor-pointer transition-all"
                            >
                              <Check className="w-4 h-4" />
                              <span>Approve</span>
                            </button>
                            <button
                              onClick={() => handleAppeal(app.id, "rejected")}
                              className="bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border border-rose-500/40 px-3 py-1.5 rounded-xl text-xs font-bold flex items-center space-x-1 cursor-pointer transition-all"
                            >
                              <X className="w-4 h-4" />
                              <span>Reject</span>
                            </button>
                          </>
                        ) : (
                          <span className={`text-xs font-mono font-bold uppercase px-3 py-1 rounded-full ${
                            app.status === "approved" ? "text-emerald-300 bg-emerald-500/20 border border-emerald-500/40" : "text-rose-300 bg-rose-500/20 border border-rose-500/40"
                          }`}>
                            Appeal {app.status}
                          </span>
                        )}
                      </div>
                    </div>
                  ))}

                  {appeals.length === 0 && (
                    <p className="text-center py-8 font-sans text-xs text-slate-500">No pending appeal tickets in queue.</p>
                  )}
                </div>
              </div>
            )}

          </div>

        </div>

      </div>
    </section>
  );
}

