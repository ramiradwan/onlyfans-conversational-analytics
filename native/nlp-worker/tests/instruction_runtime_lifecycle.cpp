// Run without model files or runtime DLL loading.
#include "../src/instruction_runtime.cpp"
#include <cassert>
#include <stdexcept>
#include <psapi.h>
#include <cstdio>
namespace {
int contexts=0, samplers=0, models=0, decodes=0, samples=0;
bool stopped=false, fail_decode=false, throw_decode=false, never_eog=false, cancel_after_prefill=false;
llama_vocab * vocab=reinterpret_cast<llama_vocab *>(1);
llama_context * ctx=reinterpret_cast<llama_context *>(2);
llama_sampler * sampler=reinterpret_cast<llama_sampler *>(3);
bool allowed(const void *) { return !stopped; }
const llama_vocab * get_vocab(const llama_model *) { return vocab; }
int32_t tokenize(const llama_vocab *,const char *,int32_t,llama_token * out,int32_t capacity,bool,bool) {
    if(!out || capacity<2)return -2;out[0]=1;out[1]=2;return 2;
}
llama_context_params context_params() {return {};}
llama_context * make_context(llama_model *,llama_context_params p) {
    assert(p.n_threads==8 && p.n_threads_batch==8 && p.n_ctx==8192);
    assert(!p.offload_kqv && !p.op_offload);return ctx;
}
uint32_t n_context(const llama_context *) {return 8192;}
llama_sampler_chain_params sampler_params() {return {};}
llama_sampler * make_chain(llama_sampler_chain_params) {return sampler;}
llama_sampler * make_grammar(const llama_vocab *,const char *,const char *) {return sampler;}
llama_sampler * make_greedy() {return sampler;}
void add_sampler(llama_sampler *,llama_sampler *) {}
void free_context(llama_context *) {++contexts;}
void free_sampler(llama_sampler *) {++samplers;}
void free_model(llama_model *) {++models;}
llama_batch batch(llama_token *,int32_t n) {llama_batch b{};b.n_tokens=n;return b;}
int32_t decode(llama_context *,llama_batch) {
    ++decodes;
    if(cancel_after_prefill)stopped=true;
    if(throw_decode)throw std::runtime_error("decode");
    if(fail_decode)return -1;
    return 0;
}
llama_token sample(llama_sampler *,llama_context *,int32_t) {
    ++samples;
    if(samples==1)return 10;
    return never_eog?10:11;
}
bool eog(const llama_vocab *,llama_token token) {return token==11;}
int32_t piece(const llama_vocab *,llama_token,char * out,int32_t,int32_t,bool) {out[0]='x';return 1;}
State * state() {
    auto * s=new State;s->threads=8;s->model=reinterpret_cast<llama_model *>(4);
    auto & a=s->api;
    a.llama_model_get_vocab=get_vocab;a.llama_tokenize=tokenize;
    a.llama_context_default_params=context_params;a.llama_init_from_model=make_context;
    a.llama_n_ctx=n_context;a.llama_sampler_chain_default_params=sampler_params;
    a.llama_sampler_chain_init=make_chain;a.llama_sampler_init_grammar=make_grammar;
    a.llama_sampler_init_greedy=make_greedy;a.llama_sampler_chain_add=add_sampler;
    a.llama_free=free_context;a.llama_sampler_free=free_sampler;a.llama_model_free=free_model;
    a.llama_batch_get_one=batch;a.llama_decode=decode;a.llama_sampler_sample=sample;
    a.llama_vocab_is_eog=eog;a.llama_token_to_piece=piece;
    return s;
}
int run(State * s,size_t capacity=65536) {
    unsigned char out[65536];size_t written=0;
    return instruction_generate(s,reinterpret_cast<const unsigned char *>("p"),1,"root ::= \"x\"",8192,192,
        out,capacity,&written,allowed,nullptr);
}
void reset() {contexts=samplers=models=decodes=samples=0;stopped=fail_decode=throw_decode=never_eog=cancel_after_prefill=false;}
}
std::string utf8(const wchar_t * value) {
    int count=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,nullptr,0,nullptr,nullptr);
    assert(count>0);std::string text(count,'\0');
    assert(WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,text.data(),count,nullptr,nullptr)==count);
    text.pop_back();return text;
}
int wmain(int argc,wchar_t ** argv) {
    if(argc>1) {
        assert(argc==12 && wcscmp(argv[1],L"--dll-probe")==0);
        assert(GetFileAttributesW(argv[11])==INVALID_FILE_ATTRIBUTES);
        assert(GetLastError()==ERROR_FILE_NOT_FOUND || GetLastError()==ERROR_PATH_NOT_FOUND);
        const wchar_t * paths[9];HMODULE before[9];
        for(int i=0;i<9;++i) {
            paths[i]=argv[i+2];const wchar_t * base=wcsrchr(paths[i],L'\\');assert(base);
            before[i]=GetModuleHandleW(base+1);
        }
        auto backend=utf8(paths[7]);auto missing=utf8(argv[11]);uint32_t stage=0;
        void * model=instruction_open(paths,backend.c_str(),missing.c_str(),8,allowed,nullptr,&stage);
        assert(!model);
        bool clean=true;
        for(int i=0;i<9;++i) {const wchar_t * base=wcsrchr(paths[i],L'\\');clean=clean && GetModuleHandleW(base+1)==before[i];}
        PROCESS_MEMORY_COUNTERS_EX memory{};memory.cb=sizeof(memory);
        assert(GetProcessMemoryInfo(GetCurrentProcess(),reinterpret_cast<PROCESS_MEMORY_COUNTERS *>(&memory),sizeof(memory)));
        std::printf("instruction_open_stage=%u cleanup=%u peak_working_set=%zu peak_private=%zu\n",stage,clean?1:0,memory.PeakWorkingSetSize,memory.PeakPagefileUsage);
        assert(clean && memory.PeakWorkingSetSize<1024ULL*1024*1024 && memory.PeakPagefileUsage<1024ULL*1024*1024);
        return stage==500?0:2;
    }
    wchar_t current[32768],short_path[32768];assert(GetModuleFileNameW(nullptr,current,32768));
    std::wstring extended=L"\\\\?\\";extended+=current;
    assert(same_file(current,extended.c_str()));
    if(GetShortPathNameW(current,short_path,32768))assert(same_file(current,short_path));

    reset();auto * s=state();assert(run(s)==0);assert(contexts==1 && samplers==1 && models==0);
    assert(samples==2);instruction_close(s);assert(models==1);
    reset();s=state();fail_decode=true;assert(run(s)==1);assert(contexts==1 && samplers==1);
    instruction_close(s);assert(models==1);
    reset();s=state();throw_decode=true;assert(run(s)==1);assert(contexts==1 && samplers==1);
    instruction_close(s);assert(models==1);
    reset();s=state();never_eog=true;assert(run(s)==3);assert(samples==192 && contexts==1 && samplers==1);
    instruction_close(s);assert(models==1);
    reset();s=state();assert(run(s,0)==3);assert(contexts==1 && samplers==1);
    instruction_close(s);assert(models==1);
    reset();s=state();stopped=true;assert(run(s)==4);assert(contexts==0 && samplers==0);
    instruction_close(s);assert(models==1);
    reset();s=state();cancel_after_prefill=true;assert(run(s)==4);assert(contexts==1 && samplers==1);
    instruction_close(s);assert(models==1);
    instruction_close(nullptr);
    assert(instruction_open(nullptr,nullptr,nullptr,8,allowed,nullptr,nullptr)==nullptr);
    return 0;
}
