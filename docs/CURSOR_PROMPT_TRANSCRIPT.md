# Cursor Prompt: Transcript Integration

Copy and paste this into Cursor for your dashboard repo:

---

I need to fetch meeting transcripts from the Koov API and display them beautifully in my dashboard with actionable items, summaries, and a clear format.

**Current situation:**
- Koov API server is running on `http://127.0.0.1:5000`
- Meetings are stored in Supabase with a `transcripts` table (or similar)
- I need to fetch transcripts during/after meetings and store them in the database
- Transcripts need to be displayed in a beautiful, easy-to-understand format

**What I need:**

1. **Fetch transcript from Koov API** (`GET http://127.0.0.1:5000/api/transcript?meeting_id=<meeting_id>`)
2. **Store transcript in Supabase** for the specific meeting and user
3. **Display transcript beautifully** with:
   - Full meeting transcript with speakers and timestamps
   - Actionable items extracted and highlighted
   - Meeting summary
   - Clean, readable format

**The Koov API transcript response:**
```json
{
  "success": true,
  "session_id": "session-20260111-190625",
  "meeting_link": "https://meet.google.com/...",
  "transcripts": [
    {
      "timestamp": 1234567890.123,
      "text": "Speaker A: Let's discuss the project timeline...",
      "tags": ["project", "timeline"],
      "segments": [
        {
          "speaker": "Speaker A",
          "start_s": 0.0,
          "end_s": 5.2,
          "text": "Let's discuss the project timeline"
        }
      ],
      "start_time": 1234567890.0,
      "end_time": 1234567895.2
    }
  ],
  "wake_cycles": [
    {
      "ts": 1234568000.0,
      "kind": "wake_cycle",
      "text": "User: summarize the meeting\nKoov: Here's a summary...",
      "meta": {
        "request": "summarize the meeting",
        "response": "Here's a summary..."
      }
    }
  ],
  "actionable_items": [
    "Review the project timeline by Friday",
    "Follow up with the design team",
    "Schedule next meeting"
  ],
  "summary": "The team discussed the project timeline and decided to...",
  "total_chunks": 15,
  "total_wake_cycles": 3
}
```

**What to implement:**

1. **Create API route to fetch and store transcript:**
   ```typescript
   // app/api/meetings/[id]/transcript/route.ts
   import { NextRequest, NextResponse } from 'next/server';
   import { createClient } from '@/lib/supabase/server';
   
   export async function GET(
     request: NextRequest,
     { params }: { params: { id: string } }
   ) {
     const meetingId = params.id;
     const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
     const supabase = createClient();
     
     try {
       // Fetch transcript from Koov API
       const response = await fetch(`${koovApiUrl}/api/transcript?meeting_id=${meetingId}`);
       const transcriptData = await response.json();
       
       if (!response.ok) {
         return NextResponse.json(transcriptData, { status: response.status });
       }
       
       // Store in Supabase
       const { error } = await supabase
         .from('transcripts') // or your transcripts table name
         .upsert({
           meeting_id: meetingId,
           session_id: transcriptData.session_id,
           transcript_data: transcriptData.transcripts,
           wake_cycles: transcriptData.wake_cycles,
           actionable_items: transcriptData.actionable_items,
           summary: transcriptData.summary,
           total_chunks: transcriptData.total_chunks,
           updated_at: new Date().toISOString(),
         }, {
           onConflict: 'meeting_id'
         });
       
       if (error) {
         throw error;
       }
       
       return NextResponse.json(transcriptData);
     } catch (error) {
       return NextResponse.json(
         { error: 'Failed to fetch transcript' },
         { status: 500 }
       );
     }
   }
   ```

2. **Poll for transcript updates during meeting:**
   ```typescript
   // In your meeting component or hook
   useEffect(() => {
     if (meetingStatus !== 'live' && meetingStatus !== 'active') return;
     
     const fetchTranscript = async () => {
       await fetch(`/api/meetings/${meetingId}/transcript`);
     };
     
     // Poll every 10 seconds during active meeting
     const interval = setInterval(fetchTranscript, 10000);
     
     return () => clearInterval(interval);
   }, [meetingId, meetingStatus]);
   ```

3. **Create beautiful transcript display component:**
   ```typescript
   // components/MeetingTranscript.tsx
   'use client';
   
   import { useState, useEffect } from 'react';
   
   interface TranscriptSegment {
     timestamp: number;
     text: string;
     tags: string[];
     segments: Array<{
       speaker: string;
       start_s: number;
       end_s: number;
       text: string;
     }>;
   }
   
   export function MeetingTranscript({ meetingId }: { meetingId: string }) {
     const [transcript, setTranscript] = useState<any>(null);
     const [loading, setLoading] = useState(true);
     
     useEffect(() => {
       const fetchTranscript = async () => {
         const response = await fetch(`/api/meetings/${meetingId}/transcript`);
         const data = await response.json();
         setTranscript(data);
         setLoading(false);
       };
       
       fetchTranscript();
     }, [meetingId]);
     
     if (loading) return <div>Loading transcript...</div>;
     if (!transcript) return <div>No transcript available</div>;
     
     const formatTime = (timestamp: number) => {
       const date = new Date(timestamp * 1000);
       return date.toLocaleTimeString();
     };
     
     return (
       <div className="space-y-6">
         {/* Summary Section */}
         {transcript.summary && (
           <div className="bg-blue-50 border-l-4 border-blue-500 p-4 rounded">
             <h3 className="font-semibold text-blue-900 mb-2">Meeting Summary</h3>
             <p className="text-blue-800">{transcript.summary}</p>
           </div>
         )}
         
         {/* Actionable Items */}
         {transcript.actionable_items && transcript.actionable_items.length > 0 && (
           <div className="bg-orange-50 border-l-4 border-orange-500 p-4 rounded">
             <h3 className="font-semibold text-orange-900 mb-3">Action Items</h3>
             <ul className="space-y-2">
               {transcript.actionable_items.map((item: string, idx: number) => (
                 <li key={idx} className="flex items-start">
                   <span className="text-orange-600 mr-2">•</span>
                   <span className="text-orange-800">{item}</span>
                 </li>
               ))}
             </ul>
           </div>
         )}
         
         {/* Full Transcript */}
         <div className="bg-white border rounded-lg p-6">
           <h3 className="font-semibold text-gray-900 mb-4">Full Transcript</h3>
           <div className="space-y-4">
             {transcript.transcripts.map((entry: TranscriptSegment, idx: number) => (
               <div key={idx} className="border-b pb-4 last:border-b-0">
                 <div className="flex items-center justify-between mb-2">
                   <span className="text-xs text-gray-500">
                     {formatTime(entry.timestamp)}
                   </span>
                   {entry.tags && entry.tags.length > 0 && (
                     <div className="flex gap-2">
                       {entry.tags.map((tag: string) => (
                         <span
                           key={tag}
                           className="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded"
                         >
                           {tag}
                         </span>
                       ))}
                     </div>
                   )}
                 </div>
                 
                 {/* Speaker segments */}
                 {entry.segments && entry.segments.length > 0 ? (
                   <div className="space-y-2">
                     {entry.segments.map((seg: any, segIdx: number) => (
                       <div key={segIdx} className="flex gap-3">
                         <span className="font-semibold text-blue-600 min-w-[100px]">
                           {seg.speaker}:
                         </span>
                         <span className="text-gray-800 flex-1">{seg.text}</span>
                       </div>
                     ))}
                   </div>
                 ) : (
                   <p className="text-gray-800">{entry.text}</p>
                 )}
               </div>
             ))}
           </div>
         </div>
         
         {/* Wake Cycles (Koov interactions) */}
         {transcript.wake_cycles && transcript.wake_cycles.length > 0 && (
           <div className="bg-purple-50 border rounded-lg p-6">
             <h3 className="font-semibold text-purple-900 mb-4">Koov Interactions</h3>
             <div className="space-y-3">
               {transcript.wake_cycles.map((cycle: any, idx: number) => (
                 <div key={idx} className="border-l-2 border-purple-400 pl-4">
                   <div className="text-xs text-gray-500 mb-1">
                     {formatTime(cycle.ts)}
                   </div>
                   <div className="text-sm text-gray-800 whitespace-pre-line">
                     {cycle.text}
                   </div>
                 </div>
               ))}
             </div>
           </div>
         )}
       </div>
     );
   }
   ```

4. **Add transcript fetching to meeting page:**
   ```typescript
   // In your meeting details page
   import { MeetingTranscript } from '@/components/MeetingTranscript';
   
   export default function MeetingDetailsPage({ params }: { params: { id: string } }) {
     // ... existing code ...
     
     return (
       <div>
         {/* Existing meeting info */}
         
         {/* Transcript section */}
         <section className="mt-8">
           <h2 className="text-2xl font-bold mb-4">Meeting Transcript</h2>
           <MeetingTranscript meetingId={params.id} />
         </section>
       </div>
     );
   }
   ```

**Please:**
1. Create the transcript API route to fetch from Koov and store in Supabase
2. Create a beautiful transcript display component with:
   - Summary section (highlighted)
   - Actionable items list (highlighted)
   - Full transcript with speakers and timestamps
   - Koov interactions section
3. Add polling to fetch transcript updates during active meetings
4. Integrate transcript display into the meeting details page
5. Make it visually appealing with proper styling
6. Show me what files were changed

**Key requirements:**
- Fetch transcript from Koov API endpoint
- Store in Supabase for the specific meeting
- Display with clear sections: Summary, Action Items, Full Transcript, Koov Interactions
- Poll for updates during active meetings
- Beautiful, readable formatting with proper styling

---
