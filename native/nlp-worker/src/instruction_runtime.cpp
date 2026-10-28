// Header ABI: llama.cpp 620d2225cc436ab0b001e3200e590e3b286ef9ab.
#include "llama.h"
#include <windows.h>
#include <new>
#include <memory>
#include <vector>
#include <string>
#include <cstring>
#include <climits>

using check_fn = bool (*)(const void *);
#define FUNCTIONS(X) \
 X(llama_model_default_params) X(llama_context_default_params) \
 X(llama_backend_init) X(llama_backend_free) X(llama_model_load_from_splits) \
 X(llama_model_free) X(llama_init_from_model) X(llama_free) X(llama_model_get_vocab) \
 X(llama_n_ctx) X(llama_tokenize) X(llama_batch_get_one) X(llama_decode) \
 X(llama_vocab_is_eog) X(llama_token_to_piece) X(llama_sampler_chain_default_params) \
 X(llama_sampler_chain_init) X(llama_sampler_chain_add) X(llama_sampler_init_greedy) \
 X(llama_sampler_init_grammar) X(llama_sampler_sample) X(llama_sampler_free) X(llama_log_set)
struct Api {
#define DECLARE(name) decltype(&::name) name = nullptr;
 FUNCTIONS(DECLARE)
 DECLARE(ggml_backend_load)
 DECLARE(ggml_backend_unload)
 DECLARE(ggml_backend_reg_count)
 DECLARE(ggml_backend_reg_dev_count)
 DECLARE(ggml_backend_reg_dev_get)
 DECLARE(ggml_backend_dev_type)
 DECLARE(ggml_log_set)
#undef DECLARE
};
struct Check { check_fn check; const void * user; };
struct State {
    HMODULE modules[9]{};
    Api api;
    Check loader{};
    ggml_backend_reg_t backend = nullptr;
    llama_model * model = nullptr;
    uint32_t threads = 2;
    bool initialized = false;
    ~State() {
        if (model) api.llama_model_free(model);
        if (initialized) api.llama_backend_free();
        if (backend) api.ggml_backend_unload(backend);
        for (int i = 8; i >= 0; --i) if (modules[i]) FreeLibrary(modules[i]);
    }
};
static bool progress(float, void * p) { auto * c = static_cast<Check *>(p); return !c->user || c->check(c->user); }
static bool abort_decode(void * p) { auto * c = static_cast<Check *>(p); return !c->check(c->user); }
static void quiet(enum ggml_log_level, const char *, void *) {}

static bool same_file(const wchar_t * left, const wchar_t * right) {
    HANDLE a=CreateFileW(left,FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,
        nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
    if(a==INVALID_HANDLE_VALUE)return false;
    HANDLE b=CreateFileW(right,FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,
        nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
    BY_HANDLE_FILE_INFORMATION x{},y{};
    bool equal=b!=INVALID_HANDLE_VALUE && GetFileInformationByHandle(a,&x) && GetFileInformationByHandle(b,&y)
        && x.dwVolumeSerialNumber==y.dwVolumeSerialNumber && x.nFileIndexHigh==y.nFileIndexHigh && x.nFileIndexLow==y.nFileIndexLow;
    if(b!=INVALID_HANDLE_VALUE)CloseHandle(b);
    CloseHandle(a);return equal;
}

extern "C" void instruction_close(void * raw) { delete static_cast<State *>(raw); }
extern "C" void * instruction_open(const wchar_t * const * paths, const char * backend,
    const char * model, uint32_t threads, check_fn check, const void * user, uint32_t * stage) {
    if (!stage) return nullptr;
    *stage=1;
    try {
    if (!paths || !backend || !model || !check || !check(user) || (threads != 2 && threads != 8)) return nullptr;
    *stage=2;
    std::unique_ptr<State> owner(new (std::nothrow) State);
    State * s = owner.get();
    if (!s) return nullptr;
    for (int i=0;i<9;++i) {
        if (!check(user)) {return nullptr;}
        *stage=100+i;
        s->modules[i] = LoadLibraryExW(paths[i], nullptr,
            LOAD_LIBRARY_SEARCH_SYSTEM32);
        if (!s->modules[i]) return nullptr;
        *stage=200+i;
        wchar_t observed[32768];
        DWORD n = s->modules[i] ? GetModuleFileNameW(s->modules[i], observed, 32768) : 0;
        if (!n || n >= 32768 || !same_file(observed, paths[i])) {return nullptr;}
    }
*stage=300;
#define RESOLVE(name) s->api.name = reinterpret_cast<decltype(s->api.name)>(GetProcAddress(s->modules[8], #name)); if (!s->api.name) {return nullptr;}
    FUNCTIONS(RESOLVE)
#undef RESOLVE
    *stage=310;
    s->api.ggml_backend_load = reinterpret_cast<decltype(s->api.ggml_backend_load)>(GetProcAddress(s->modules[6], "ggml_backend_load"));
    s->api.ggml_backend_unload = reinterpret_cast<decltype(s->api.ggml_backend_unload)>(GetProcAddress(s->modules[6], "ggml_backend_unload"));
    if (!s->api.ggml_backend_load || !s->api.ggml_backend_unload) {return nullptr;}
#define GGML_RESOLVE(name, index) s->api.name = reinterpret_cast<decltype(s->api.name)>(GetProcAddress(s->modules[index], #name)); if (!s->api.name) {return nullptr;}
    GGML_RESOLVE(ggml_backend_reg_count,6)
    GGML_RESOLVE(ggml_backend_reg_dev_count,5)
    GGML_RESOLVE(ggml_backend_reg_dev_get,5)
    GGML_RESOLVE(ggml_backend_dev_type,5)
    GGML_RESOLVE(ggml_log_set,5)
#undef GGML_RESOLVE
    s->api.llama_log_set(quiet, nullptr);
    s->api.ggml_log_set(quiet, nullptr);
    *stage=400;
    if (s->api.ggml_backend_reg_count()!=0) {return nullptr;}
    *stage=401;
    s->backend = s->api.ggml_backend_load(backend);
    if (!s->backend) return nullptr;
    *stage=402;
    if (!s->backend || s->api.ggml_backend_reg_count()!=1 ||
        s->api.ggml_backend_reg_dev_count(s->backend)!=1 ||
        s->api.ggml_backend_dev_type(s->api.ggml_backend_reg_dev_get(s->backend,0))!=GGML_BACKEND_DEVICE_TYPE_CPU) {return nullptr;}
    *stage=403;
    s->api.llama_backend_init();s->initialized=true;s->threads=threads;
    s->loader={check,user};
    auto params=s->api.llama_model_default_params();
    params.n_gpu_layers=0;params.use_extra_bufts=false;
    params.progress_callback=progress;params.progress_callback_user_data=&s->loader;
    *stage=500;
    const char * bound_paths[]={model};
    s->model=s->api.llama_model_load_from_splits(bound_paths,1,params);
    if (!s->model || !check(user)) {return nullptr;}
    s->loader.user=nullptr;
    *stage=0;
    return owner.release();
    } catch (...) { *stage=900; return nullptr; }
}
struct Session {
    State * s;
    llama_context * context = nullptr;
    llama_sampler * sampler = nullptr;
    ~Session() {if(sampler)s->api.llama_sampler_free(sampler);if(context)s->api.llama_free(context);}
};
// 0: complete EOG, 1: failure, 2: input bound, 3: output bound, 4: stopped.
extern "C" int instruction_generate(void * raw, const unsigned char * prompt, size_t length,
    const char * grammar, uint32_t context, uint32_t output, unsigned char * buffer,
    size_t capacity, size_t * written, check_fn check, const void * user) {
    try {
    auto * s=static_cast<State *>(raw);
    if(!s||!prompt||!grammar||!buffer||!written||!check||context!=8192||output!=192||length>1048576)return 1;
    *written=0;if(!check(user))return 4;
    auto * vocab=s->api.llama_model_get_vocab(s->model);if(!vocab)return 1;
    int32_t count=s->api.llama_tokenize(vocab,reinterpret_cast<const char *>(prompt),static_cast<int32_t>(length),nullptr,0,true,true);
    if(count==INT32_MIN||count>=0)return 1;
    count=-count;if(count<=0||static_cast<uint32_t>(count)+output>context)return 2;
    std::vector<llama_token> tokens(count);
    if(s->api.llama_tokenize(vocab,reinterpret_cast<const char *>(prompt),static_cast<int32_t>(length),tokens.data(),count,true,true)!=count)return 1;
    if(!check(user))return 4;
    Check control{check,user};Session session{s};
    auto cp=s->api.llama_context_default_params();
    cp.n_ctx=context;cp.n_batch=256;cp.n_ubatch=256;cp.n_seq_max=1;
    cp.n_threads=s->threads;cp.n_threads_batch=s->threads;
    cp.offload_kqv=false;cp.op_offload=false;
    cp.abort_callback=abort_decode;cp.abort_callback_data=&control;
    session.context=s->api.llama_init_from_model(s->model,cp);
    if(!session.context||!check(user))return 1;
    if(s->api.llama_n_ctx(session.context)<context)return 2;
    auto sp=s->api.llama_sampler_chain_default_params();sp.no_perf=true;
    session.sampler=s->api.llama_sampler_chain_init(sp);if(!session.sampler)return 1;
    auto * constrained=s->api.llama_sampler_init_grammar(vocab,grammar,"root");if(!constrained)return 1;
    s->api.llama_sampler_chain_add(session.sampler,constrained);
    auto * greedy=s->api.llama_sampler_init_greedy();if(!greedy)return 1;
    s->api.llama_sampler_chain_add(session.sampler,greedy);
    for(int32_t at=0;at<count;) {
        if(!check(user))return 4;
        int32_t n=(count-at)>256?256:count-at;
        auto batch=s->api.llama_batch_get_one(tokens.data()+at,n);
        if(s->api.llama_decode(session.context,batch)!=0)return 1;
        at+=n;
    }
    for(uint32_t generated=0;generated<output;++generated) {
        if(!check(user))return 4;
        auto token=s->api.llama_sampler_sample(session.sampler,session.context,-1);
        if(token<0)return 1;
        if(s->api.llama_vocab_is_eog(vocab,token))return generated+1<output?0:3;
        char piece[65536];
        int32_t size=s->api.llama_token_to_piece(vocab,token,piece,sizeof(piece),0,false);
        if(size<0||static_cast<size_t>(size)>capacity-*written)return 3;
        std::memcpy(buffer+*written,piece,size);*written+=size;
        if(generated+1==output)return 3;
        auto batch=s->api.llama_batch_get_one(&token,1);
        if(s->api.llama_decode(session.context,batch)!=0)return 1;
    }
    return 3;
    } catch (...) { return 1; }
}
