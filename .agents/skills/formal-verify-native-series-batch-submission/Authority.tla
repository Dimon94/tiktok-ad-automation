---------------- MODULE Authority ----------------
EXTENDS Naturals, TLC
CONSTANT Bug
VARIABLE s
vars == <<s>>
Init == s = [epoch |-> 1, approvedEpoch |-> 0, generation |-> 1, active |-> TRUE, connection |-> 1,
 binding |-> 1, revision |-> 1, stamp |-> 0, approvedRoute |-> <<1,1,1>>,
 phase |-> "new", held |-> FALSE, admitted |-> <<0,0,0,0>>, safe |-> TRUE,
 unrelated |-> 0]
Current == <<s.generation,s.connection,s.binding,s.revision>>
Approved == <<s.stamp,s.approvedRoute[1],s.approvedRoute[2],s.approvedRoute[3]>>
Valid == s.active /\ (s.stamp = s.generation \/ Bug = "stamp")
 /\ (s.approvedRoute = <<s.connection,s.binding,s.revision>> \/ Bug = "route")
 /\ (s.stamp # 0 \/ Bug = "null")
Approve == /\ s.phase = "new" /\ s.active
 /\ s' = [s EXCEPT !.stamp = s.generation, !.approvedEpoch = s.epoch,
 !.approvedRoute = <<s.connection,s.binding,s.revision>>, !.phase = "approved"]
Legacy == /\ s.phase = "new" /\ s' = [s EXCEPT !.phase = "approved"]
Queue == /\ s.phase = "approved" /\ (Valid \/ (Bug = "null" /\ s.stamp = 0))
 /\ s' = [s EXCEPT !.phase = "queued"]
Claim == /\ s.phase = "queued" /\ s' = [s EXCEPT !.phase = "claimed"]
Arm == /\ s.phase = "claimed" /\ s' = [s EXCEPT !.phase = "armed"]
Lock == /\ s.phase = "armed" /\ (Valid \/ (Bug = "null" /\ s.stamp = 0))
 /\ s' = [s EXCEPT !.held = TRUE, !.admitted = Current, !.phase = "locked"]
Send == /\ s.phase = "locked" /\ s.held
 /\ s' = [s EXCEPT !.phase = "sent", !.safe =
     (s.safe /\ s.active /\ Approved = Current /\ s.stamp # 0 /\ s.epoch = s.approvedEpoch)]
Exit == /\ s.phase \in {"locked","sent"}
 /\ s' = [s EXCEPT !.held = FALSE, !.phase = "unknown"]
Receipt == /\ s.phase = "sent"
 /\ s' = [s EXCEPT !.held = FALSE, !.phase = "receipt"]
Changeable == ~s.held \/ Bug = "lock"
Revoke == /\ Changeable /\ s.active /\ s.generation < 3
 /\ s' = [s EXCEPT !.active = FALSE, !.epoch = 2, !.generation = @ + 1]
Regrant == /\ Changeable /\ ~s.active /\ s.generation < 3
 /\ s' = [s EXCEPT !.active = TRUE, !.epoch = 3, !.generation = IF Bug = "aba" THEN 1 ELSE @ + 1]
Route(k) == /\ Changeable /\ s[k] = 1
 /\ s' = [s EXCEPT ![k] = 2]
Unrelated == /\ s.unrelated = 0 /\ s' = [s EXCEPT !.unrelated = 1]
Next == Approve \/ Legacy \/ Queue \/ Claim \/ Arm \/ Lock \/ Send \/ Exit \/ Receipt
 \/ Revoke \/ Regrant \/ Unrelated \/ (\E k \in {"connection","binding","revision"}: Route(k))
FinalFence == s.safe
HeldAuthority == s.held => Current = s.admitted
=============================================================================
