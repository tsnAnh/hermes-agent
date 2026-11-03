---- MODULE CronOccurrences ----
EXTENDS Naturals, FiniteSets

CONSTANTS None, N, MaxCrashes, MaxGen,
          StampOnDispatch, ClaimClearsStamp, AdvanceBeforeDispatch,
          HonorLiveForeignStamp, StampLeaseCanExpire, RunsFinishWithinSlot, TickIsInstant

Slots == 1..N
Procs == {"gateway", "crontab"}
Incarnations == 1..MaxGen
Owners == Procs \X Incarnations

VARIABLES now, next, stamp, stampExpired, claim, fired, settled,
          alive, gen, held, tickLock, phase, disp, crashes

vars == <<now, next, stamp, stampExpired, claim, fired, settled,
          alive, gen, held, tickLock, phase, disp, crashes>>

Me(p) == <<p, gen[p]>>
IdAlive(id) == alive[id[1]] /\ gen[id[1]] = id[2]
Span(a, b) == {s \in Slots : a <= s /\ s <= b}
Queued(p) == held[p] # None /\ held[p].step = "queued"
Running(p) == held[p] # None /\ held[p].step = "running"

TypeOK ==
    /\ now \in 0..N
    /\ next \in 1..(N + 1)
    /\ stamp \in [slot : Slots, by : Owners] \cup {None}
    /\ stampExpired \in BOOLEAN
    /\ claim \in Owners \cup {None}
    /\ fired \in [Slots -> 0..2]
    /\ settled \subseteq Slots
    /\ alive \in [Procs -> BOOLEAN]
    /\ gen \in [Procs -> Incarnations]
    /\ held \in [Procs -> [slot : Slots, step : {"queued", "running"}] \cup {None}]
    /\ tickLock \in Procs \cup {None}
    /\ phase \in [Procs -> {"idle", "locked", "scanned", "advanced"}]
    /\ disp \in [Procs -> 0..N]
    /\ crashes \in 0..MaxCrashes

Init ==
    /\ now = 0
    /\ next = 1
    /\ stamp = None
    /\ stampExpired = FALSE
    /\ claim = None
    /\ fired = [s \in Slots |-> 0]
    /\ settled = {}
    /\ alive = [p \in Procs |-> TRUE]
    /\ gen = [p \in Procs |-> 1]
    /\ held = [p \in Procs |-> None]
    /\ tickLock = None
    /\ phase = [p \in Procs |-> "idle"]
    /\ disp = [p \in Procs |-> 0]
    /\ crashes = 0

AdvanceTime ==
    /\ now < N
    /\ TickIsInstant => \A p \in Procs : phase[p] \notin {"scanned", "advanced"}
    /\ RunsFinishWithinSlot => \A p \in Procs : ~Running(p)
    /\ now' = now + 1
    /\ UNCHANGED <<next, stamp, stampExpired, claim, fired, settled,
                   alive, gen, held, tickLock, phase, disp, crashes>>

ExpireStamp ==
    /\ StampLeaseCanExpire
    /\ stamp # None
    /\ ~stampExpired
    /\ stampExpired' = TRUE
    /\ UNCHANGED <<now, next, stamp, claim, fired, settled,
                   alive, gen, held, tickLock, phase, disp, crashes>>

Crash(p) ==
    /\ alive[p]
    /\ crashes < MaxCrashes
    /\ alive' = [alive EXCEPT ![p] = FALSE]
    /\ held' = [held EXCEPT ![p] = None]
    /\ phase' = [phase EXCEPT ![p] = "idle"]
    /\ tickLock' = IF tickLock = p THEN None ELSE tickLock
    /\ crashes' = crashes + 1
    /\ UNCHANGED <<now, next, stamp, stampExpired, claim, fired, settled, gen, disp>>

Restart(p) ==
    /\ ~alive[p]
    /\ gen[p] < MaxGen
    /\ alive' = [alive EXCEPT ![p] = TRUE]
    /\ gen' = [gen EXCEPT ![p] = gen[p] + 1]
    /\ UNCHANGED <<now, next, stamp, stampExpired, claim, fired, settled,
                   held, tickLock, phase, disp, crashes>>

AcquireTickLock(p) ==
    /\ alive[p]
    /\ phase[p] = "idle"
    /\ tickLock = None
    /\ tickLock' = p
    /\ phase' = [phase EXCEPT ![p] = "locked"]
    /\ UNCHANGED <<now, next, stamp, stampExpired, claim, fired, settled,
                   alive, gen, held, disp, crashes>>

GetDueJobs(p) ==
    /\ phase[p] = "locked"
    /\ LET running == held[p] # None
           foreign == stamp # None /\ stamp.by # Me(p)
           honored == HonorLiveForeignStamp /\ foreign /\ IdAlive(stamp.by) /\ ~stampExpired
           restore == stamp # None /\ ~running /\ ~honored
           base == IF restore THEN stamp.slot ELSE next
           kept == IF restore THEN None ELSE stamp
           due == base <= now
           stale == now - base >= 2
           keptExpired == IF kept = None THEN FALSE ELSE stampExpired
       IN \/ /\ ~due
             /\ next' = base
             /\ stamp' = kept
             /\ stampExpired' = keptExpired
             /\ phase' = [phase EXCEPT ![p] = "idle"]
             /\ tickLock' = None
             /\ UNCHANGED <<settled, disp>>
          \/ /\ due
             /\ stale
             /\ next' = now + 1
             /\ stamp' = kept
             /\ stampExpired' = keptExpired
             /\ settled' = settled \cup Span(1, now)
             /\ phase' = [phase EXCEPT ![p] = "idle"]
             /\ tickLock' = None
             /\ UNCHANGED disp
          \/ /\ due
             /\ next' = IF stale THEN now + 1 ELSE base
             /\ stamp' = IF StampOnDispatch THEN [slot |-> base, by |-> Me(p)] ELSE kept
             /\ stampExpired' = IF StampOnDispatch THEN FALSE ELSE keptExpired
             /\ disp' = [disp EXCEPT ![p] = base]
             /\ phase' = [phase EXCEPT ![p] = "scanned"]
             /\ UNCHANGED <<settled, tickLock>>
    /\ UNCHANGED <<now, claim, fired, alive, gen, held, crashes>>

AdvanceNextRuns(p) ==
    /\ phase[p] = "scanned"
    /\ next' = IF AdvanceBeforeDispatch THEN now + 1 ELSE next
    /\ phase' = [phase EXCEPT ![p] = "advanced"]
    /\ UNCHANGED <<now, stamp, stampExpired, claim, fired, settled,
                   alive, gen, held, tickLock, disp, crashes>>

SubmitWithGuard(p) ==
    /\ phase[p] = "advanced"
    /\ phase' = [phase EXCEPT ![p] = "idle"]
    /\ tickLock' = None
    /\ IF held[p] # None
          THEN /\ settled' = settled \cup Span(disp[p], now)
               /\ UNCHANGED held
          ELSE /\ held' = [held EXCEPT ![p] = [slot |-> disp[p], step |-> "queued"]]
               /\ UNCHANGED settled
    /\ UNCHANGED <<now, next, stamp, stampExpired, claim, fired,
                   alive, gen, disp, crashes>>

ClaimJobForFire(p) ==
    /\ alive[p]
    /\ Queued(p)
    /\ LET d == held[p].slot
       IN IF claim # None /\ IdAlive(claim)
             THEN /\ held' = [held EXCEPT ![p] = None]
                  /\ settled' = settled \cup Span(d, now)
                  /\ UNCHANGED <<next, stamp, stampExpired, claim, fired>>
             ELSE /\ claim' = Me(p)
                  /\ stamp' = IF ClaimClearsStamp THEN None ELSE stamp
                  /\ stampExpired' = IF ClaimClearsStamp THEN FALSE ELSE stampExpired
                  /\ next' = now + 1
                  /\ fired' = [fired EXCEPT ![d] = IF @ < 2 THEN @ + 1 ELSE 2]
                  /\ settled' = settled \cup Span(1, now)
                  /\ held' = [held EXCEPT ![p] = [slot |-> d, step |-> "running"]]
    /\ UNCHANGED <<now, alive, gen, tickLock, phase, disp, crashes>>

MarkJobRun(p) ==
    /\ alive[p]
    /\ Running(p)
    /\ claim = Me(p)
    /\ claim' = None
    /\ stamp' = None
    /\ stampExpired' = FALSE
    /\ next' = now + 1
    /\ settled' = settled \cup Span(next, now)
    /\ held' = [held EXCEPT ![p] = None]
    /\ UNCHANGED <<now, fired, alive, gen, tickLock, phase, disp, crashes>>

Next ==
    \/ AdvanceTime
    \/ ExpireStamp
    \/ \E p \in Procs :
           \/ Crash(p)
           \/ Restart(p)
           \/ AcquireTickLock(p)
           \/ GetDueJobs(p)
           \/ AdvanceNextRuns(p)
           \/ SubmitWithGuard(p)
           \/ ClaimJobForFire(p)
           \/ MarkJobRun(p)

Spec == Init /\ [][Next]_vars

AtMostOnce == \A s \in Slots : fired[s] <= 1

Unsettled == {s \in Slots : s <= now /\ s < next /\ s \notin settled}

DispatchInFlight(p) ==
    /\ alive[p]
    /\ \/ phase[p] \in {"scanned", "advanced"}
       \/ Queued(p)

NoSilentLoss == Unsettled # {} => (stamp # None \/ \E p \in Procs : DispatchInFlight(p))

WitnessOrphanStamp == ~(stamp # None /\ ~IdAlive(stamp.by))

WitnessClaimLost == ~(\E p \in Procs : Queued(p) /\ claim # None /\ IdAlive(claim))

WitnessDispatchRefused == ~(\E p \in Procs : phase[p] = "advanced" /\ held[p] # None)

====
