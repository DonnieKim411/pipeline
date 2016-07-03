%{
fields.DirectionalTrial (computed) # directional drift trials
-> fields.Directional
drift_trial     : smallint               # trial index
---
-> psy.Trial
direction                   : float                         # (degrees) direction of drift
onset                       : double                        # (s) onset time in rf.Sync times
offset                      : double                        # (s) offset time in rf.Sync times
%}


classdef DirectionalTrial < dj.Relvar
end