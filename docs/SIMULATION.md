# Simulation bench

The service loop can run without a plant. The simulated path is a small fleet of motors whose vibration is synthesised from a fixed seed, a real HTTP stand-in of
the data lake that serves it, a real TCP stand-in of the broker that receives the alarms, and the real detector, registry, DATALAKE client and follower of this
repository. Failures are injected on purpose: a bearing that wears out little by little, a data lake that is not there, a data lake that refuses the verdicts, a broker
that refuses the connection, a hole in the sampling and a sensor that sends a value that is not a number.

## Run it

```
python -m pytest -q tests/test_simulation_bench.py
```

## What the scenarios check

| Scenario | What it shows |
|----------|---------------|
| a fleet with one worn motor | only the worn motor is flagged, every window of it |
| a bearing that wears out | the wear is caught once and the verdict does not flicker back to healthy; the score grows with the wear |
| the same seed twice, another seed | the run is reproducible and really depends on the seed |
| a data lake that is not there | the errors are counted, the loop survives and says why |
| a data lake that refuses the verdicts | the scoring goes on, the write errors are counted, the next verdict is written once it recovers |
| a broker that refuses the connection | the alarms are counted as lost, the verdicts still reach the data lake |
| a hole in the sampling | a window is never made of two pieces |
| a sensor that sends garbage | the other motors are not affected |
| a motor without a baseline | it is never scored against the baseline of another motor |

The workflow named "Simulation bench" runs this file on every change, so the simulated path has its own visible job.

## What it cannot show

How the detector behaves on a real machine, with its real noise, its real bearings and its real load changes, needs a real machine. The status of the repository
therefore stays at "simulated" for the hardware row until a person has run the service on the bench.
