# RE Agent Debugger Overview

- Mean IRE is 0.90 but content coverage is only 0.48, indicating systematic blind spot for content-type requirements
- Style coverage is 0.86 but 3 tasks completely missed style requirements because the agent never asked about visual design
- approx_ESR is only 0.17, meaning the agent is very inefficient per turn—requirements are found early then many turns are wasted on details
- missed_content accounts for 11 of 17 failure tags, making it the dominant failure mode
- Agent frequently falls into detail rabbit holes: after finding a requirement it spends 5-10 turns on implementation specifics instead of broadening search
- one_question_guard fires repeatedly but is consistently ignored by the agent
- style_requirement_guard fires and helps when heeded, but the agent sometimes never reaches style questions at all
