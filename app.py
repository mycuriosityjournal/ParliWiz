import streamlit as st
import pandas as pd
from streamlit_gsheets import GSheetsConnection
import altair as alt
import google.generativeai as genai
import json
import hashlib
from datetime import datetime
import os

# --- CONFIGURATION & AUTH ---
st.set_page_config(
    page_title="ParliWiz - FBLA Prep", 
    page_icon="⚖️", 
    layout="wide"
)

# 1. Setup Gemini
if "gemini" in st.secrets:
    genai.configure(api_key=st.secrets["gemini"]["api_key"])
else:
    st.error("Missing Gemini API Key in secrets.toml")

# 2. Connect to Google Sheets
conn = st.connection("gsheets", type=GSheetsConnection)

# --- AUTHENTICATION ---
def simple_auth():
    if 'authenticated' not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            try:
                st.image("logo.png", use_container_width=True)
            except:
                st.header("🧙 ParliWiz")
            
            st.markdown("### FBLA Competitor Login")
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            
            if st.button("Login", use_container_width=True):
                try:
                    users_df = conn.read(worksheet="Users", ttl=0)
                    users_df['Username'] = users_df['Username'].astype(str)
                    users_df['Password'] = users_df['Password'].astype(str)

                    user_match = users_df[users_df['Username'] == username]
                    
                    if not user_match.empty:
                        stored_hash = user_match.iloc[0]['Password'].strip()
                        input_hash = hashlib.sha256(password.encode()).hexdigest()
                        
                        if input_hash == stored_hash:
                            st.session_state.authenticated = True
                            st.session_state.user = username
                            st.success("Login Successful!")
                            st.rerun()
                        else:
                            st.error("Incorrect Password")
                    else:
                        st.error("User not found")
                        
                except Exception as e:
                    st.error(f"Login System Error: {e}")
        st.stop() 

simple_auth()

# --- SIDEBAR ---
with st.sidebar:
    try:
        st.image("logo.png", use_container_width=True)
    except:
        st.header("🧙 ParliWiz")
        
    st.markdown(f"**User:** {st.session_state.user}")
    
    if st.button("🚪 Logout", type="primary"):
        st.session_state.authenticated = False
        st.session_state.user = None
        st.session_state.score = 0
        st.session_state.history = []
        st.session_state.current_questions = []
        st.session_state.active_question = None
        st.session_state.reveal_answer = False
        st.rerun()

    st.markdown("---")
    
    CATEGORIES = [
        "The Basics (Quorum, Agenda)",
        "Handling Motions (Main, Subsidiary)", 
        "Debate & Amendments",
        "Voting & Elections",
        "Officers & Committees"
    ]
    
    selected_category = st.selectbox("Generate Questions For:", CATEGORIES)
    
    # Slider for number of questions
    num_questions = st.slider("Number of Questions:", min_value=1, max_value=10, value=5)
    
    if st.button("✨ Generate New Questions"):
        st.session_state.trigger_gen = True
        st.session_state.active_question = None 
        st.session_state.reveal_answer = False
        st.rerun()
    
    st.markdown("---")
    if st.button("Reset Stats (Keep Login)"):
        st.session_state.score = 0
        st.session_state.total_answered = 0
        st.session_state.history = []
        st.session_state.active_question = None
        st.session_state.reveal_answer = False
        st.rerun()

# Initialize Session State
if 'score' not in st.session_state:
    st.session_state.score = 0
    st.session_state.total_answered = 0
    st.session_state.history = []
    st.session_state.current_questions = []
    
if 'active_question' not in st.session_state:
    st.session_state.active_question = None
if 'reveal_answer' not in st.session_state:
    st.session_state.reveal_answer = False

# --- HELPER FUNCTIONS ---

def get_master_prompt():
    """Fetches the prompt text from the 'Config' tab in Google Sheets."""
    try:
        df = conn.read(worksheet="Config", ttl=0, header=None)
        mask = df[0].astype(str).str.strip() == "Master_Prompt"
        row = df[mask]
        if not row.empty:
            return row.iloc[0, 1]
        else:
            st.error("Config Error: 'Master_Prompt' not found.")
            return None
    except Exception as e:
        st.error(f"Error reading prompt: {e}")
        return None

def get_recent_questions(user, limit=15):
    """Fetches the last N questions answered by this user to avoid repetition."""
    try:
        df = conn.read(worksheet="Questions", ttl=0)
        user_df = df[df['User'] == user]
        if not user_df.empty:
            return user_df.tail(limit)['Question'].tolist()
        return []
    except Exception:
        return []

def log_to_sheet(user, category, question, user_choice, correct_answer, is_correct, explanation):
    """Appends the question result to the 'Questions' tab in Google Sheets."""
    try:
        try:
            df = conn.read(worksheet="Questions", ttl=0)
        except:
            df = pd.DataFrame(columns=[
                "Timestamp", "User", "Category", "Question", 
                "User_Choice", "Correct_Answer", "Is_Correct", "Explanation"
            ])

        new_data = {
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "User": user,
            "Category": category,
            "Question": question,
            "User_Choice": user_choice,
            "Correct_Answer": correct_answer,
            "Is_Correct": "Yes" if is_correct else "No",
            "Explanation": explanation
        }
        
        new_row_df = pd.DataFrame([new_data])
        updated_df = pd.concat([df, new_row_df], ignore_index=True)
        conn.update(worksheet="Questions", data=updated_df)
        
    except Exception as e:
        st.warning(f"Could not log to Google Sheet: {e}")

# --- NEW: SMART ANSWER CHECKING FUNCTION ---
def check_answer(user_selection, correct_key):
    """
    Robustly compares the user's selection with the correct answer key.
    Handles cases like:
    - User: "C. Text" vs Key: "C" (Letter match)
    - User: "Text" vs Key: "Text." (Punctuation mismatch)
    - User: "Text" vs Key: "text" (Case mismatch)
    """
    if not user_selection or not correct_key:
        return False
        
    # 1. Normalize strings
    u = str(user_selection).strip()
    c = str(correct_key).strip()
    
    # 2. Extract potential letter (e.g. "A. Option" -> "A")
    # Split by "." or ")" to handle "A." or "A)" formats
    u_letter = u.split(".")[0].split(")")[0].strip().upper()
    c_letter = c.split(".")[0].split(")")[0].strip().upper()
    
    # 3. Strategy A: Exact Letter Match (if Key is just A/B/C/D)
    if len(c_letter) == 1 and c_letter in ["A", "B", "C", "D"]:
        return u_letter == c_letter

    # 4. Strategy B: Full Text Match (Ignore Case/Punctuation)
    if u.lower().rstrip(".") == c.lower().rstrip("."):
        return True
        
    # 5. Strategy C: Containment (Handle partial matches)
    if c.lower() in u.lower():
        return True
        
    return False

# --- AI GENERATION LOGIC ---

def generate_questions_with_gemini(category, num_q):
    base_prompt_text = get_master_prompt()
    if not base_prompt_text: 
        return pd.DataFrame()
    
    recent_qs = get_recent_questions(st.session_state.user)
    exclusion_text = ""
    if recent_qs:
        exclusion_list = "\n - ".join(recent_qs)
        exclusion_text = f"\nAVOID REPEATING THESE RECENT QUESTIONS:\n{exclusion_list}\n"

    try:
        # Prompt tweaked to encourage cleaner JSON
        final_prompt = f"""
        {base_prompt_text}
        
        Use your internal knowledge of "Robert's Rules of Order Newly Revised (12th Edition)" and the "In Brief (3rd Edition)".
        
        {exclusion_text}
        
        SPECIFIC TASK:
        Generate {num_q} difficult multiple-choice questions for the category: "{category}"
        Output STRICT VALID JSON. Each answer key should be the OPTION LETTER (A, B, C, or D) if possible, or the exact text.
        """
        
        with st.spinner(f"Consulting the Parliamentarian for {num_q} fresh questions..."):
            model = genai.GenerativeModel('gemini-2.5-flash') 
            response = model.generate_content(final_prompt)
        
        clean_text = response.text.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]
            
        data = json.loads(clean_text)
        return pd.DataFrame(data)

    except Exception as e:
        st.error(f"AI Generation Failed: {e}")
        return pd.DataFrame()

# --- MAIN APP LOGIC ---

if 'trigger_gen' in st.session_state and st.session_state.trigger_gen:
    new_df = generate_questions_with_gemini(selected_category, num_questions)
    if not new_df.empty:
        st.session_state.current_questions = new_df
        st.session_state.trigger_gen = False
        st.session_state.active_question = None 
        st.session_state.reveal_answer = False

if isinstance(st.session_state.current_questions, pd.DataFrame) and not st.session_state.current_questions.empty:
    
    if st.session_state.active_question is None:
        st.session_state.active_question = st.session_state.current_questions.sample(1).iloc[0]
        st.session_state.reveal_answer = False
    
    question = st.session_state.active_question

    with st.container(border=True):
        st.subheader(question['Question'])
        st.caption(f"📂 {question['Category']}")
        
        q_key = hash(question['Question'])
        
        user_choice = st.radio(
            "Select your answer:", 
            question["Options"], 
            index=None, 
            key=q_key,
            disabled=st.session_state.reveal_answer 
        )
        
        # --- SUBMIT BUTTON ---
        if not st.session_state.reveal_answer:
            if st.button("Submit Answer", type="primary"):
                if user_choice:
                    st.session_state.reveal_answer = True
                    
                    # USE THE NEW SMART CHECKER
                    is_correct = check_answer(user_choice, question["Answer"])

                    st.session_state.total_answered += 1
                    if is_correct:
                        st.session_state.score += 1
                    
                    st.session_state.history.append({
                        "Category": question['Category'],
                        "Result": "Correct" if is_correct else "Incorrect"
                    })
                    
                    log_to_sheet(
                        user=st.session_state.user,
                        category=question['Category'],
                        question=question['Question'],
                        user_choice=user_choice,
                        correct_answer=question['Answer'],
                        is_correct=is_correct,
                        explanation=question['Explanation']
                    )
                    
                    st.rerun()
                else:
                    st.warning("Please select an option first.")

        # --- FEEDBACK & NEXT BUTTON ---
        if st.session_state.reveal_answer:
            # Re-check for display
            is_correct = check_answer(user_choice, question["Answer"])
            
            if is_correct:
                st.success("✅ Correct!")
            else:
                st.error(f"❌ Incorrect. The correct answer was: {question['Answer']}")
            
            st.info(f"📘 **Explanation:** {question['Explanation']}")
            
            if st.button("Next Question ➡️", type="primary"):
                answered_q_text = st.session_state.active_question['Question']
                
                # Filter out the answered question
                st.session_state.current_questions = st.session_state.current_questions[
                    st.session_state.current_questions['Question'] != answered_q_text
                ]
                
                st.session_state.active_question = None
                st.session_state.reveal_answer = False
                st.rerun()

elif isinstance(st.session_state.current_questions, pd.DataFrame) and st.session_state.current_questions.empty:
    if 'history' in st.session_state and len(st.session_state.history) > 0:
        st.success("🎉 You have completed this batch! Click 'Generate New Questions' to continue.")
    else:
        st.info("👈 Select a category, choose the number of questions, and click 'Generate'!")

# --- ANALYTICS ---
if len(st.session_state.history) > 0:
    st.markdown("---")
    st.subheader("📊 Wizard Stats")
    
    history_df = pd.DataFrame(st.session_state.history)
    category_stats = history_df.groupby("Category")["Result"].apply(lambda x: (x == "Correct").mean()).reset_index()
    category_stats.columns = ["Category", "Accuracy"]
    weakest_link = category_stats.sort_values("Accuracy").iloc[0]
    
    col1, col2 = st.columns([1, 2])
    with col1:
        st.metric("Total Mastery", f"{st.session_state.score}/{st.session_state.total_answered}")
        if weakest_link['Accuracy'] < 0.5:
             st.error(f"⚠️ **Study Alert:** Review **{weakest_link['Category']}**")
        else:
             st.success(f"🌟 **Top Skill:** You are acing **{weakest_link['Category']}**")

    with col2:
        chart = alt.Chart(category_stats).mark_bar().encode(
            x=alt.X('Accuracy', axis=alt.Axis(format='%')),
            y=alt.Y('Category', sort='-x'),
            color=alt.condition(
                alt.datum.Accuracy < 0.6,
                alt.value('#FF4B4B'),
                alt.value('#00CC96')
            )
        )
        st.altair_chart(chart, use_container_width=True)