---------------- MODULE Completion ----------------
\* Focused, bounded model of native_completion.batch_results after R1.
\* One plan is Campaign -> AdGroup -> (K-2) Ads, plus one representative
\* non-READBACK preparation step. Create readback and creative MATCH are facts.
\* "pending" folds PENDING/QUEUED/RUNNING/RETRYABLE only for the live guard;
\* the real HTTP regression exercises all four persisted statuses. Pending
\* objects have no remote ID. Local cancellation is a persisted FAILED status
\* with local_submission_cancelled, hence cancelled parents seed the closure.
EXTENDS Naturals, FiniteSets
CONSTANT N, K, Bug

Plans == 1..N
Objects == Plans \X (1..K)
AdObjects == {o \in Objects: o[2] >= 3}
PlanObjects(p) == {o \in Objects: o[1] = p}
ParentOf(o) == <<o[1], IF o[2] = 2 THEN 1 ELSE 2>>

VARIABLE prep, phase, readback, creative
vars == <<prep, phase, readback, creative>>

Init ==
 /\ prep = [p \in Plans |-> "pending"]
 /\ phase = [o \in Objects |-> "pending"]
 /\ readback = [o \in Objects |-> "absent"]
 /\ creative = [o \in Objects |-> FALSE]

Prepare(p, r) ==
 /\ prep[p] = "pending"
 /\ r \in {"succeeded", "failed", "unknown", "mismatch"}
 /\ prep' = [prep EXCEPT ![p] = r]
 /\ UNCHANGED <<phase, readback, creative>>

ParentCreated(o) ==
 /\ prep[o[1]] = "succeeded"
 /\ IF o[2] = 1 THEN TRUE ELSE phase[ParentOf(o)] = "succeeded"

FinishCreate(o, r) ==
 /\ phase[o] = "pending"
 /\ ParentCreated(o)
 /\ r \in {"succeeded", "failed", "unknown"}
 /\ phase' = [phase EXCEPT ![o] = r]
 /\ readback' = [readback EXCEPT ![o] = IF r = "succeeded" THEN "waiting" ELSE @]
 /\ UNCHANGED <<prep, creative>>

Read(o, r) ==
 /\ phase[o] = "succeeded"
 /\ readback[o] = "waiting"
 /\ r \in {"matched", "failed", "unknown", "mismatch"}
 /\ readback' = [readback EXCEPT ![o] = r]
 /\ UNCHANGED <<prep, phase, creative>>

Creative(o) ==
 /\ o \in AdObjects
 /\ readback[o] = "matched"
 /\ ~creative[o]
 /\ creative' = [creative EXCEPT ![o] = TRUE]
 /\ UNCHANGED <<prep, phase, readback>>

\* All create objects are still unsent. Preparation may already have succeeded;
\* this is the source's all-create local_submission_cancelled case.
CancelCreates(p) ==
 /\ \A o \in PlanObjects(p): phase[o] = "pending" /\ readback[o] = "absent"
 /\ phase' = [o \in Objects |-> IF o[1] = p THEN "cancelled" ELSE phase[o]]
 /\ UNCHANGED <<prep, readback, creative>>

\* dispatch._dependency persists blocked descendants later. The receipt derives
\* the same closure first, so waiting for that write cannot hide a terminal plan.
FailedClosure(p) ==
 LET F0 == {o \in PlanObjects(p): phase[o] \in {"failed", "cancelled"}}
     Direct(F) == {o \in PlanObjects(p):
                    phase[o] = "pending" /\ ParentOf(o) \in F}
     F1 == F0 \cup Direct(F0)
     F2 == F1 \cup Direct(F1)
     F3 == F2 \cup Direct(F2)
 IN F3

RecordBlocked(o) ==
 /\ phase[o] = "pending"
 /\ \/ o \in FailedClosure(o[1])
    \/ o[2] = 1 /\ prep[o[1]] = "failed"
 /\ phase' = [phase EXCEPT ![o] = "failed"]
 /\ UNCHANGED <<prep, readback, creative>>

Complete(p) ==
 \A o \in PlanObjects(p):
  /\ phase[o] = "succeeded"
  /\ readback[o] = "matched"
  /\ o[2] < 3 \/ creative[o]

HasReview(p) ==
 \/ prep[p] \in {"unknown", "mismatch"}
 \/ \E o \in PlanObjects(p):
      phase[o] \in {"unknown", "mismatch"}
      \/ readback[o] \in {"unknown", "mismatch"}

AllStepsCancelled(p) ==
 /\ prep[p] = "cancelled"
 /\ \A o \in PlanObjects(p): phase[o] = "cancelled"

AllCreatesCancelled(p) ==
 \A o \in PlanObjects(p): phase[o] = "cancelled"

PlanCancelled(p) == AllStepsCancelled(p) \/ AllCreatesCancelled(p)
LiveOutsideClosure(p) ==
 \/ prep[p] = "pending"
 \/ \E o \in PlanObjects(p):
      phase[o] = "pending" /\ o \notin FailedClosure(p)

HasFailure(p) ==
 \/ prep[p] \in {"failed", "cancelled"}
 \/ \E o \in PlanObjects(p):
      phase[o] \in {"failed", "cancelled"} \/ o \in FailedClosure(p)

StepState(x) ==
 CASE x = "pending" -> "PENDING"
  [] x = "succeeded" -> "SUCCEEDED"
  [] x = "matched" -> "SUCCEEDED"
  [] x = "failed" -> "FAILED"
  [] x = "cancelled" -> "FAILED"
  [] x = "unknown" -> "UNKNOWN"
  [] x = "mismatch" -> "MISMATCH"
  [] OTHER -> "QUEUED"

AggregateStates(p) ==
 LET reads == {o \in PlanObjects(p): readback[o] # "absent"}
 IN {StepState(prep[p])}
    \cup {StepState(IF o \in FailedClosure(p) THEN "failed" ELSE phase[o]):
          o \in PlanObjects(p)}
    \cup {StepState(readback[o]): o \in reads}

Aggregate(S) ==
 IF S \cap {"UNKNOWN", "MISMATCH"} # {} THEN "NEEDS_REVIEW"
 ELSE IF "PENDING" \in S THEN "RUNNING"
 ELSE IF S = {"SUCCEEDED"} THEN "COMPLETED"
 ELSE IF "SUCCEEDED" \in S /\ "FAILED" \in S THEN "PARTIAL"
 ELSE IF S = {"FAILED"} THEN "FAILED"
 ELSE "QUEUED"

LegacyResult(p) ==
 LET state == Aggregate(AggregateStates(p))
 IN IF state = "COMPLETED" THEN "NEEDS_REVIEW"
    ELSE IF state = "PARTIAL" THEN "FAILED"
    ELSE state

\* ignoreLive=TRUE is the R1 defect: omit only the closure exclusion in the
\* live-sibling guard. Every later terminal fallback remains reachable.
PlanResult(p, ignoreLive) ==
 IF Complete(p) THEN "COMPLETED"
 ELSE IF HasReview(p) THEN "NEEDS_REVIEW"
 ELSE IF PlanCancelled(p) THEN "CANCELLED"
 ELSE IF LiveOutsideClosure(p) /\ ~ignoreLive THEN "RUNNING"
 ELSE IF HasFailure(p) THEN
   IF \E o \in PlanObjects(p): phase[o] = "succeeded" THEN "PARTIAL" ELSE "FAILED"
 ELSE LegacyResult(p)

BatchResult(ignoreLive) ==
 LET results == [p \in Plans |-> PlanResult(p, ignoreLive)]
     states == {results[p]: p \in Plans}
 IN IF "NEEDS_REVIEW" \in states THEN "NEEDS_REVIEW"
    ELSE IF states \cap {"RUNNING", "QUEUED"} # {} THEN "RUNNING"
    ELSE IF states = {"CANCELLED"} THEN "CANCELLED"
    ELSE IF states = {"COMPLETED"} THEN "COMPLETED"
    ELSE IF "COMPLETED" \in states THEN "PARTIAL"
    ELSE IF states = {"PARTIAL"} /\ N = 1 THEN "PARTIAL"
    ELSE "FAILED"

ActualReport == BatchResult(Bug = "live-sibling")
BatchReview == \E p \in Plans: HasReview(p)
\* Cancellation and review take precedence over the live guard. A pending
\* preparation step of an already-cancelled plan is not a runnable sibling.
BatchLive == \E p \in Plans:
  /\ ~Complete(p) /\ ~HasReview(p) /\ ~PlanCancelled(p)
  /\ LiveOutsideClosure(p)

\* Independent single-plan terminal partial shape (the existing M1 receipt
\* compatibility exception). It excludes the live sibling before aggregation.
SinglePlanPartial ==
  /\ N = 1
  /\ ~Complete(1) /\ ~HasReview(1) /\ ~PlanCancelled(1)
  /\ ~LiveOutsideClosure(1) /\ HasFailure(1)
  /\ \E o \in PlanObjects(1): phase[o] = "succeeded"

\* The focused R1 counterexample: a failed Ad and a still-runnable Ad coexist.
\* A correct receipt must keep the plan (and therefore this batch) RUNNING.
FailedAdWithLiveSibling(p) ==
  /\ ~Complete(p) /\ ~HasReview(p) /\ ~PlanCancelled(p)
  /\ \E f \in AdObjects: f[1] = p /\ phase[f] = "failed"
  /\ \E live \in AdObjects:
       live[1] = p /\ phase[live] = "pending" /\ live \notin FailedClosure(p)
  /\ \E s \in PlanObjects(p): phase[s] = "succeeded"
LiveSiblingError ==
  /\ Bug = "live-sibling"
  /\ \E p \in Plans: FailedAdWithLiveSibling(p)
  /\ ActualReport \in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}
LiveSiblingSafety == ~LiveSiblingError

\* Independent terminal contract, not an alias for PlanResult/BatchResult.
\* READBACK waiting may legitimately remain nonterminal after creates finish.
\* The single-plan PARTIAL receipt is the existing M1 compatibility exception;
\* multi-plan PARTIAL still requires at least one fully MATCHed plan.
TerminalContract(r) ==
 /\ (BatchReview => r = "NEEDS_REVIEW")
 /\ (r \in {"COMPLETED", "PARTIAL", "FAILED"} =>
      ~BatchReview /\ ~BatchLive)
 /\ (r = "COMPLETED" => \A p \in Plans: Complete(p))
 /\ (r = "PARTIAL" =>
      \/ \E p \in Plans: Complete(p)
      \/ SinglePlanPartial)
 /\ (r = "FAILED" => ~(\E p \in Plans: Complete(p)))
 /\ (r = "CANCELLED" => ~BatchReview /\ \A p \in Plans: PlanCancelled(p))

Completion == TerminalContract(ActualReport)

\* Expected-error reachability witness, not a product safety failure. Both AdGroups
\* failed and all their Ads are still unrecorded. The closure must make the two
\* plans PARTIAL and the batch FAILED, not leave a runnable sibling RUNNING.
BlockedDescendantWitness ==
 /\ N = 2
 /\ \A p \in Plans:
      /\ prep[p] = "succeeded"
      /\ phase[<<p, 1>>] = "succeeded"
      /\ phase[<<p, 2>>] = "failed"
      /\ readback[<<p, 1>>] = "waiting"
      /\ \A o \in AdObjects: o[1] = p => phase[o] = "pending"
 /\ ~BatchLive
 /\ ActualReport = "FAILED"
NoBlockedDescendantTerminal == ~BlockedDescendantWitness

Next ==
 \/ \E p \in Plans:
      \/ \E r \in {"succeeded", "failed", "unknown", "mismatch"}: Prepare(p, r)
      \/ CancelCreates(p)
 \/ \E o \in Objects:
      \/ \E r \in {"succeeded", "failed", "unknown"}: FinishCreate(o, r)
      \/ \E r \in {"matched", "failed", "unknown", "mismatch"}: Read(o, r)
      \/ Creative(o)
      \/ RecordBlocked(o)

=============================================================================
