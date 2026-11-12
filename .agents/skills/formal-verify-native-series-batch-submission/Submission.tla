----------------------------- MODULE Submission -----------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANT Plans
Objects == Plans \X {1,2,3,4}
VARIABLES submissions, phase, sends, valid, frozen, body, matched, authorized, budget
vars == <<submissions, phase, sends, valid, frozen, body, matched, authorized,budget>>
Init == /\ submissions = 0
        /\ phase = [p \in Objects |-> "pending"]
        /\ sends = [p \in Objects |-> 0]
        /\ valid = TRUE /\ frozen = 1
        /\ body = [p \in Objects |-> 0]
        /\ authorized = [p \in Objects |-> TRUE]
        /\ budget = 20050 /\ matched = {}
Submit == /\ submissions = 0 /\ valid
          /\ submissions' = 1
          /\ UNCHANGED <<phase,sends,valid,frozen,body,matched,authorized,budget>>
Invalidate == /\ valid /\ valid' = FALSE
              /\ UNCHANGED <<submissions,phase,sends,frozen,body,matched,authorized,budget>>
Arm(p) == /\ submissions = 1 /\ valid /\ phase[p] = "pending"
          /\ (IF p[2] = 1 THEN TRUE ELSE phase[<<p[1],p[2]-1>>] = "created")
          /\ phase' = [phase EXCEPT ![p] = "armed"]
          /\ body' = [body EXCEPT ![p] = frozen]
          /\ UNCHANGED <<submissions,sends,valid,frozen,matched,authorized,budget>>
Send(p) == /\ valid /\ phase[p] = "armed" /\ body[p] = frozen
           /\ phase' = [phase EXCEPT ![p] = "sent"]
           /\ sends' = [sends EXCEPT ![p] = @ + 1]
           /\ authorized' = [authorized EXCEPT ![p] = valid]
           /\ UNCHANGED <<submissions,valid,frozen,body,matched,budget>>
Finish(p, result) == /\ phase[p] = "sent"
                     /\ phase' = [phase EXCEPT ![p] = result]
                     /\ UNCHANGED <<submissions,sends,valid,frozen,body,matched,authorized,budget>>
Read(p) == /\ phase[p] = "created"
           /\ matched' = matched \cup {p}
           /\ UNCHANGED <<submissions,phase,sends,valid,frozen,body,authorized,budget>>
Next == Submit \/ Invalidate \/ (\E p \in Objects: Arm(p) \/ Send(p) \/ Read(p) \/ (\E r \in {"created", "failed", "unknown"}: Finish(p,r)))
UniqueSubmission == submissions <= 1
WriteOnce == \A p \in Objects: sends[p] <= 1
FrozenBody == \A p \in Objects: body[p] \in {0, frozen}
MatchedCreated == \A p \in matched: phase[p] = "created" /\ sends[p] = 1
CompletedPlans == {p \in Plans: \A k \in 1..4: <<p,k>> \in matched}
Complete == CompletedPlans = Plans
Completion == Complete => \A p \in Objects: phase[p] = "created"
FinalFence == \A p \in Objects: authorized[p]
ExactBudget == budget = 10025 + 10025
=============================================================================
