---------------- MODULE Submission ----------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANT N, K, Bug
Plans == 1..N
Objects == Plans \X (1..K)
AdObjects == {o \in Objects: o[2] >= 3}
VARIABLE s
vars == <<s>>
Init == s = [submissions |-> 0, frozen |-> FALSE, intent |-> 1, snapshot |-> 0,
 budget |-> 0, amount |-> 1, steps |-> {},
 phase |-> [o \in Objects |-> "pending"], body |-> [o \in Objects |-> 0],
 sends |-> [o \in Objects |-> 0], ids |-> {}, matched |-> {}, creatives |-> {},
 historical |-> {}, negativeOK |-> TRUE, recoveryOK |-> TRUE, reportOK |-> TRUE,
 cancelled |-> FALSE, sendAfterCancel |-> FALSE]
Edit == /\ s.intent = 1 /\ s' = [s EXCEPT !.intent = 2]
Freeze(a) == /\ ~s.frozen /\ a \in {1,2}
 /\ s' = [s EXCEPT !.frozen = TRUE, !.snapshot = s.intent, !.amount = a,
 !.budget = IF Bug = "budget" THEN N*a*K ELSE N*a]
Submit == /\ s.frozen /\ s.intent = s.snapshot
 /\ (s.submissions = 0 \/ (Bug = "unique" /\ s.submissions = 1))
 /\ s' = [s EXCEPT !.submissions = @ + 1, !.steps =
     IF Bug = "excluded" THEN Objects \cup {<<N+1,1>>} ELSE Objects]
Parent(o) == IF o[2] = 1 THEN TRUE ELSE <<o[1],IF o[2] = 2 THEN 1 ELSE 2>> \in s.ids
Queue(o) == /\ ~s.cancelled /\ s.submissions > 0 /\ s.phase[o] = "pending" /\ Parent(o)
 /\ s.intent = s.snapshot /\ s' = [s EXCEPT !.phase[o] = "queued"]
Claim(o) == /\ ~s.cancelled /\ s.phase[o] = "queued" /\ s' = [s EXCEPT !.phase[o] = "claimed"]
SendAdmissionOpen == ~s.cancelled
Arm(o) == /\ SendAdmissionOpen /\ s.phase[o] = "claimed"
 /\ s' = [s EXCEPT !.phase[o] = "armed", !.body[o] = s.snapshot]
Lock(o) == /\ s.phase[o] = "armed" /\ s.intent = s.snapshot
 /\ s' = [s EXCEPT !.phase[o] = "locked"]
Send(o) == /\ (SendAdmissionOpen \/ (Bug = "cancel-fence" /\ s.cancelled)) /\ s.phase[o] = "locked"
 /\ s' = [s EXCEPT !.phase[o] = "sent", !.sends[o] = @ + 1,
          !.sendAfterCancel = @ \/ s.cancelled]
Finish(o,r) == /\ s.phase[o] = "sent" /\ r \in {"created","failed","unknown"}
 /\ s' = [s EXCEPT !.phase[o] = r, !.ids = IF r = "created" THEN @ \cup {o} ELSE @]
\* R11: cancel marks the Submission once and converts only idle pending work.
LocalCancel == /\ ~s.cancelled /\ s' = [s EXCEPT !.cancelled = TRUE]
CancelPending(o) == /\ s.cancelled
 /\ ((s.phase[o] = "pending" /\ s.body[o] = 0 /\ s.sends[o] = 0 /\ o \notin s.matched)
    \/ Bug = "cancel-armed")
 /\ s' = [s EXCEPT !.phase[o] = "cancelled"]
Crash(o) == /\ s.phase[o] \in {"armed","locked"}
 /\ s' = [s EXCEPT !.phase[o] = "unknown"]
Retry(o) == /\ Bug = "retry" /\ s.phase[o] = "unknown" /\ s.sends[o] = 1
 /\ s' = [s EXCEPT !.phase[o] = "queued"]
Recover(o,count,full,scope) == /\ s.phase[o] = "unknown"
 /\ count \in {0,1,2} /\ full \in BOOLEAN /\ scope \in BOOLEAN
 /\ LET claim == (count = 1 /\ full /\ scope) \/ (Bug = "recovery" /\ count = 2 /\ full /\ scope)
     IN s' = [s EXCEPT !.phase[o] = IF claim THEN "created" ELSE @,
          !.ids = IF claim THEN @ \cup {o} ELSE @,
          !.matched = IF claim THEN @ \cup {o} ELSE @,
          !.recoveryOK = @ /\ (claim => count = 1 /\ full /\ scope)]
\* Historical input only: same sent attempt retained an explicit allowed rejection.
HistoricalReceipt(o) == /\ s.phase[o] = "sent" /\ o[2] >= 2
 /\ s' = [s EXCEPT !.phase[o] = "unknown", !.historical = @ \cup {o}]
RejectHistorical(o) == /\ s.phase[o] = "unknown"
 /\ (o \in s.historical \/ Bug = "negativeproof")
 /\ s' = [s EXCEPT !.phase[o] = "failed",
                  !.negativeOK = @ /\ o \in s.historical]
Read(o) == /\ o \in s.ids /\ s' = [s EXCEPT !.matched = @ \cup {o}]
Creative(o) == /\ o \in s.ids \cap AdObjects /\ s' = [s EXCEPT !.creatives = @ \cup {o}]
Tamper(o) == /\ Bug = "body" /\ s.body[o] # 0
 /\ s' = [s EXCEPT !.body[o] = 3]
Complete(p) == \A k \in 1..K: <<p,k>> \in s.matched /\ (k < 3 \/ <<p,k>> \in s.creatives)
Publish == /\ s.submissions > 0
 /\ LET done == {p \in Plans: Complete(p)}
         unknown == \E o \in Objects: s.phase[o] = "unknown"
         failed == {p \in Plans: \E k \in 1..K: s.phase[<<p,k>>] \in {"failed","cancelled"}}
         cancelledPlans == {p \in Plans: \A k \in 1..K: s.phase[<<p,k>>] = "cancelled"}
         reported == IF Bug = "completion" THEN "COMPLETED"
                     ELSE IF unknown THEN "NEEDS_REVIEW"
                     ELSE IF cancelledPlans = Plans THEN "CANCELLED"
                     ELSE IF done = Plans THEN "COMPLETED"
                     ELSE IF done \cup failed = Plans THEN
                         IF done = {} THEN "FAILED" ELSE "PARTIAL"
                     ELSE "RUNNING"
     IN s' = [s EXCEPT !.reportOK =
       (@ /\ (reported = "COMPLETED" => ~unknown /\ s.ids = Objects
                 /\ s.matched = Objects /\ s.creatives = AdObjects)
          /\ (reported = "PARTIAL" => ~unknown /\ done # {} /\ done # Plans)
          /\ (reported = "FAILED" => ~unknown /\ done = {})
          /\ (reported = "CANCELLED" => ~unknown /\ cancelledPlans = Plans)
          /\ (unknown => reported = "NEEDS_REVIEW"))]

Next == Edit \/ (\E a \in {1,2}: Freeze(a)) \/ Submit \/ Publish \/ LocalCancel \/
 (\E o \in Objects: Queue(o) \/ Claim(o) \/ Arm(o) \/ Lock(o) \/ Send(o) \/ CancelPending(o)
 \/ (\E count \in {0,1,2}, full \in BOOLEAN, scope \in BOOLEAN: Recover(o,count,full,scope))
 \/ HistoricalReceipt(o) \/ RejectHistorical(o)
 \/ Crash(o) \/ Retry(o) \/ Read(o) \/ Creative(o) \/ Tamper(o)
 \/ (\E r \in {"created","failed","unknown"}: Finish(o,r)))
UniqueSubmission == s.submissions <= 1
WriteOnce == \A o \in Objects: s.sends[o] <= 1
CancelOnlyIdle == \A o \in Objects: s.phase[o] = "cancelled" =>
 /\ s.body[o] = 0 /\ s.sends[o] = 0 /\ o \notin s.matched
CancelFence == ~s.sendAfterCancel
FrozenBody == \A o \in Objects: s.body[o] \in {0,s.snapshot}
ExactBudget == s.frozen => s.budget = N*s.amount
Excluded == s.steps \subseteq Objects
NoHistoricalTerminal == ~(s.historical # {} /\
 (\A p \in Plans: \E k \in 1..K: <<p,k>> \in s.historical /\ s.phase[<<p,k>>] = "failed") /\
 (\A o \in Objects: s.phase[o] # "unknown") /\
 (\A p \in Plans: ~Complete(p)))
NegativeProof == s.negativeOK
HistoricalPreservation == [][(\E o \in Objects: RejectHistorical(o)) =>
 /\ s.ids = s'.ids /\ s.sends = s'.sends /\ s.body = s'.body
 /\ s.snapshot = s'.snapshot]_vars
Recovery == s.recoveryOK
Completion == s.reportOK
=============================================================================
