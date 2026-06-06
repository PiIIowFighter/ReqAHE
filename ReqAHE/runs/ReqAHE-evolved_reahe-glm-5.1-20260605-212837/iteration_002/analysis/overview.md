# RE Agent Debugger Overview

- Main score improved from 0.548 to 0.709; IRE up from 0.517 to 0.65; approx_ESR up from 0.444 to 0.778
- Style coverage is perfect at 1.0 but content coverage remains low at 0.333 and interaction coverage dropped to 0.389
- One-question guard fired in 6 of 9 turns yet agent never corrected compound-question behavior
- 2 of 3 tasks tagged missed_content; content-type requirements systematically neglected
- Agent finished early in train_000195 with one turn remaining, leaving 2 content requirements undiscovered
- No duplicate, broad, or invalid questions observed — agent maintains question quality on those dimensions
