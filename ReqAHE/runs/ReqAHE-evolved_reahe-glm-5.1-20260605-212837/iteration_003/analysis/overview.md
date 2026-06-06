# RE Agent Debugger Overview

- Main score regressed from 0.709 to 0.569; IRE dropped from 0.65 to 0.52, TKQR from 0.82 to 0.67.
- Style coverage fell from 1.0 to 0.67; content coverage flat at 0.33; interaction coverage flat at 0.39.
- Compound questions triggered one_question_guard on 5 of 9 turns but only in warn mode—no actual blocking.
- Missed_content tagged twice, missed_style once; content-type requirements (customer service, responsive design, registration/login) consistently unprobed.
- Oracle deflections ('I'm not sure') on turns 1-2 of train_000195 and train_000483 were not followed by category pivots.
